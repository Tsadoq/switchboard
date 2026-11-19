import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq
from agent_board.night import queue_arm as qa
from agent_board.night import queue_association as qs
from agent_board.night.journal_latch import CleanupOutcome
from agent_board.night_state import ClockReading

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue_scale import hidden_parents
    from packages.agent_board.tests.test_night_effective_queue import CONNECT
    from packages.agent_board.tests.test_night_effective_queue_scale import history
    from packages.agent_board.tests.test_night_queue_association import (
        BOOT,
        FENCE,
        GRANT,
        GUARD,
        NOW,
        UID,
        Live,
        live,
        ok,
    )
else:
    from test_night_capture_queue_scale import hidden_parents
    from test_night_effective_queue import CONNECT
    from test_night_effective_queue_scale import history
    from test_night_queue_association import BOOT, FENCE, GRANT, GUARD, NOW, UID, Live, live, ok

__all__ = ["live"]
CONNECT_ORIGINAL = sqlite3.connect


class Tape:
    """Every attempted statement on the reader's two connections, with injected faults."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail_at: int | None = None,
        when: str = "before",
        error: tuple[object, str | int] | None = None,
        error_when: str = "before",
        rollback: dict[str, str] | None = None,
        close: dict[str, str] | None = None,
        rows: tuple[str, int, Any] | None = None,
    ) -> None:
        self.attempts: list[str] = []
        self.native: list[str] = []
        self.skipped: list[int] = []
        self.connections: list[sqlite3.Connection] = []
        self.closes = 0
        self.rollbacks: dict[str, int] = {}
        tape = self
        reads: dict[str, int] = {}

        def failure() -> sqlite3.Error:
            raised = sqlite3.OperationalError("database is locked")
            if error is not None and error[0] is not None:
                raised.sqlite_errorcode = error[0]  # type: ignore[assignment]
            return raised

        class Cursor:
            def __init__(self, cursor: sqlite3.Cursor, statement: str) -> None:
                self.cursor, self.statement = cursor, statement

            def __getattr__(self, name: str) -> Any:
                return getattr(self.cursor, name)

            def fetchall(self) -> list[Any]:
                result = self.cursor.fetchall()
                if rows is None or self.statement != rows[0]:
                    return result
                reads[self.statement] = reads.get(self.statement, 0) + 1
                if reads[self.statement] != rows[1]:
                    return result
                changed: list[Any] = rows[2](result)
                return changed

        class Connection(sqlite3.Connection):
            role = ""

            def execute(self, sql: str, parameters: Any = (), /) -> Any:
                index = len(tape.attempts)
                tape.attempts.append(sql)
                if error is not None and error[1] == index and error_when == "before":
                    tape.skipped.append(index)
                    raise failure()
                if error is not None and error[1] == index:
                    super().execute(sql, parameters)
                    raise failure()
                fault = when if index == fail_at else None
                if sql == "ROLLBACK":
                    tape.rollbacks[self.role] = tape.rollbacks.get(self.role, 0) + 1
                    fault = (rollback or {}).get(self.role, fault)
                if fault == "before":
                    tape.skipped.append(index)
                    raise sqlite3.OperationalError("injected before execute")
                cursor = super().execute(sql, parameters)
                if fault == "after":
                    raise sqlite3.OperationalError("injected after execute")
                return Cursor(cursor, sql)

            def close(self) -> None:
                tape.closes += 1
                fault = (close or {}).get(self.role)
                if fault == "before":
                    raise sqlite3.OperationalError("injected before close")
                super().close()
                if fault == "after":
                    raise sqlite3.OperationalError("injected after close")

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            assert kwargs == {"uri": True, "isolation_level": None, "timeout": 0}
            assert args[0].endswith("?mode=ro")
            role = "journal" if len(tape.connections) == 0 else "ledger"
            if error is not None and error[1] == f"connect-{role}":
                raise failure()
            conn = CONNECT_ORIGINAL(*args, **kwargs, factory=Connection)
            conn.role = role
            conn.set_trace_callback(tape.native.append)
            tape.connections.append(conn)
            return conn

        monkeypatch.setattr(sqlite3, "connect", connect)

    def dispose(self) -> None:
        for conn in self.connections:
            sqlite3.Connection.close(conn)


def unchanged(live: Live) -> tuple[bytes, bytes]:
    return live.journal.read_bytes(), live.ledger.read_bytes()


def no_association(result: qs.AssociationResult) -> None:
    assert result.association is None, result


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("index", range(25))
def test_a_failure_at_any_statement_returns_no_association_and_closes_both(
    live: Live, monkeypatch: pytest.MonkeyPatch, index: int, when: str
) -> None:
    before = unchanged(live)
    tape = Tape(monkeypatch, fail_at=index, when=when)

    result = live.read()
    tape.dispose()

    no_association(result)
    statement = tape.attempts[index]
    assert result.fault == (qs.Fault.ROLLBACK_FAILURE if statement == "ROLLBACK" else qs.Fault.SQLITE_FAILURE)
    assert result.refusal is None
    assert tape.closes == len(tape.connections) == 2
    assert all(count <= 1 for count in tape.rollbacks.values())
    assert len(tape.native) + len(tape.skipped) == len(tape.attempts) <= 25
    rolled_back = {23: "ledger", 24: "journal"}.get(index) if statement == "ROLLBACK" else None
    assert result.journal_cleanup == (
        CleanupOutcome.UNCERTAIN if rolled_back == "journal" else CleanupOutcome.CLOSED
    )
    assert result.ledger_cleanup == (
        CleanupOutcome.UNCERTAIN if rolled_back == "ledger" else CleanupOutcome.CLOSED
    )
    assert unchanged(live) == before


@pytest.mark.parametrize(
    ("rollback_role", "close_role"),
    [(r, c) for r in (None, "journal", "ledger") for c in (None, "journal", "ledger") if r or c],
)
@pytest.mark.parametrize("when", ["before", "after"])
def test_cleanup_faults_keep_per_file_status_and_close_first_precedence(
    live: Live, monkeypatch: pytest.MonkeyPatch, rollback_role: str | None, close_role: str | None, when: str
) -> None:
    tape = Tape(
        monkeypatch,
        rollback={rollback_role: when} if rollback_role else None,
        close={close_role: when} if close_role else None,
    )

    result = live.read()
    tape.dispose()

    no_association(result)
    assert result.fault == (qs.Fault.CLOSE_FAILURE if close_role else qs.Fault.ROLLBACK_FAILURE)
    uncertain = {rollback_role, close_role}
    assert result.journal_cleanup == (
        CleanupOutcome.UNCERTAIN if "journal" in uncertain else CleanupOutcome.CLOSED
    )
    assert result.ledger_cleanup == (
        CleanupOutcome.UNCERTAIN if "ledger" in uncertain else CleanupOutcome.CLOSED
    )
    assert tape.closes == 2 and tape.rollbacks == {"journal": 1, "ledger": 1}


@pytest.mark.parametrize("code", [None, True, "5", 5, 6, 261, 262, 517, -251, -250, -5])
@pytest.mark.parametrize(
    ("stage", "when"),
    [
        ("connect-journal", "before"),
        ("connect-ledger", "before"),
        (0, "before"),
        (3, "before"),
        (3, "after"),
        (4, "before"),
        (7, "before"),
        (7, "after"),
        (8, "before"),
    ],
)
def test_only_typed_busy_before_any_successful_begin_is_busy(
    live: Live, monkeypatch: pytest.MonkeyPatch, code: Any, stage: str | int, when: str
) -> None:
    tape = Tape(monkeypatch, error=(code, stage), error_when=when)

    result = live.read()
    tape.dispose()

    early = (stage, when) in (
        ("connect-journal", "before"),
        ("connect-ledger", "before"),
        (0, "before"),
        (3, "before"),
    )
    busy = early and type(code) is int and code >= 0 and code & 0xFF in (5, 6)
    assert result.refusal == (qs.Refusal.BUSY if busy else None), result
    assert result.fault == (None if busy else qs.Fault.SQLITE_FAILURE)
    no_association(result)
    assert tape.closes == len(tape.connections)
    for conn in tape.connections:
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(conn, "SELECT 1")


def mutate_column(column: str) -> Any:
    def apply(rows: list[Any]) -> list[Any]:
        if len(rows) != 1:
            return rows
        row = dict(rows[0])
        value = row[column]
        if value is None:
            row[column], row[f"{column}_kind"] = b"x", "text"
        elif isinstance(value, bytes):
            row[column] = (b"b" if value[:1] == b"a" else b"a") + value[1:]
        elif isinstance(value, float):
            row[column] = value + 1.0
        else:
            row[column] = value + 1
        return [row]

    return apply


READS = [
    (qs.RUN_QUERY, 1, list(qs._RUN)),
    (qs.ACTIVE_QUERY, 1, ["run_id"]),
    (eq.BINDING_QUERY, 1, list(eq._BINDING)),
    (cq.CAPTURES_QUERY, 1, list(cq._CAPTURES)),
    (cq.RECEIPT_BY_ID_QUERY, 1, list(eq._RECEIPT)),
    (qa.ARM_QUERY, 1, list(qa._ARM)),
    (eq.IDENTITY_QUERY, 1, list(eq._IDENTITY)),
    (eq.CONTROL_QUERY, 1, list(eq._CONTROL)),
]


@pytest.mark.parametrize(
    ("query", "number", "column"),
    [
        pytest.param(query, number, column, id=f"{query.split(' FROM ')[1].split()[0]}-{column}")
        for query, number, columns in READS
        for column in columns
        if (query, column) != (cq.RECEIPT_BY_ID_QUERY, "seq")
    ],
)
def test_every_column_of_every_read_is_checked(
    live: Live, monkeypatch: pytest.MonkeyPatch, query: str, number: int, column: str
) -> None:
    tape = Tape(monkeypatch, rows=(query, number, mutate_column(column)))
    result = live.read()
    tape.dispose()
    no_association(result)
    assert result.refusal is not None and result.fault is None, result


@pytest.mark.parametrize("shape", ["empty", "duplicate"])
@pytest.mark.parametrize(
    "query", [query for query, _, _ in READS] + [pytest.param(cq.LINK_BY_PARENT_QUERY, id="link")]
)
def test_every_read_with_the_wrong_cardinality_refuses(
    live: Live, monkeypatch: pytest.MonkeyPatch, query: str, shape: str
) -> None:
    if query == cq.LINK_BY_PARENT_QUERY and shape == "empty":
        ok(live.read())
        return

    def apply(rows: list[Any]) -> list[Any]:
        if query == cq.LINK_BY_PARENT_QUERY:
            return [("x",)] if shape == "duplicate" else rows
        return [] if shape == "empty" else rows * 2

    tape = Tape(monkeypatch, rows=(query, 1, apply))
    result = live.read()
    tape.dispose()
    no_association(result)
    assert result.refusal is not None, result


def deleted(value: Any, name: str) -> Any:
    object.__delattr__(value, name)
    return value


@pytest.mark.parametrize(
    ("argument", "field"),
    [
        ("expected", "fence"),
        ("expected", "uid"),
        ("now", "utc"),
        ("guard", "guard_invocation_id"),
        ("completion", "snapshot"),
        ("control", "acked_counter"),
    ],
)
def test_a_deleted_input_field_is_invalid_before_any_sql(
    live: Live, monkeypatch: pytest.MonkeyPatch, argument: str, field: str
) -> None:
    values: dict[str, Any] = {
        "expected": GRANT,
        "now": NOW,
        "guard": GUARD,
        "completion": live.completion,
        "control": live.control,
    }
    tape = Tape(monkeypatch)
    result = live.read(**{argument: deleted(replace(values[argument]), field)})
    tape.dispose()
    assert result.refusal == qs.Refusal.INVALID_INPUT and tape.attempts == []


def test_inputs_mutated_during_the_open_do_not_change_the_result(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = ok(live.read())
    guard, grant = replace(GUARD), replace(GRANT)
    real_open = os.open

    def open_and_tamper(path: Any, *args: Any, **kwargs: Any) -> int:
        object.__setattr__(guard, "guard_invocation_id", "d" * 32)
        object.__setattr__(grant, "uid", UID + 1)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_and_tamper)
    assert ok(live.read(guard=guard, expected=grant)) == expected


def test_no_reader_descriptor_or_connection_survives_any_outcome(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open, real_close = os.open, os.close
    owned: set[int] = set()

    def track_open(path: Any, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, *args, **kwargs)
        if Path(path) in (live.journal, live.ledger):
            owned.add(fd)
        return fd

    def track_close(fd: int) -> None:
        real_close(fd)
        owned.discard(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", track_close)
    tape = Tape(monkeypatch)
    for change in ({}, {"now": ClockReading(BOOT, 100.0, 1999.0)}, {"grace": 5}):
        live.read(**change)
    assert owned == set()
    for conn in tape.connections:
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(conn, "SELECT 1")


def stopped_runs(path: Path, size: int) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,'stopped',1,0,0,NULL,1)",
            (
                (f"{n:08x}-0000-4000-8000-{n:012x}", FENCE, "pi@dev", UID, BOOT, 10.0, 20.0)
                for n in range(size)
            ),
        )
        conn.execute("COMMIT")


@pytest.mark.parametrize("size", [10, 10_000, 100_000])
def test_the_schedule_is_25_at_any_history(live: Live, monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    stopped_runs(live.journal, size)
    history(live.ledger, size)
    hidden_parents(live.ledger, size // 3)
    tape = Tape(monkeypatch)
    ok(live.read())
    tape.dispose()
    assert len(tape.attempts) == len(tape.native) == 25


@pytest.mark.parametrize("query", [qs.RUN_QUERY, qs.ACTIVE_QUERY])
@pytest.mark.parametrize("hit", [True, False])
def test_journal_lookups_use_an_index_with_constant_cost(live: Live, query: str, hit: bool) -> None:
    ticks = [0]

    def tick() -> int:
        ticks[0] += 1
        return 0

    target = GRANT.run_id if hit else "a0000000-0000-4000-8000-000000000000"
    parameters: tuple[object, ...] = (target,) if query == qs.RUN_QUERY else ()
    steps: list[int] = []
    for size, start in ((10, 0), (10_000, 10)):
        with closing(CONNECT(live.journal, isolation_level=None)) as conn:
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,'stopped',1,0,0,NULL,1)",
                (
                    (f"{n:08x}-0000-4000-8000-{n:012x}", FENCE, "pi@dev", UID, BOOT, 10.0, 20.0)
                    for n in range(start, size)
                ),
            )
            conn.execute("COMMIT")
        with closing(CONNECT(live.journal)) as conn:
            plan = " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {query}", parameters))
            ticks[0] = 0
            conn.set_progress_handler(tick, 1)
            conn.execute(query, parameters).fetchall()
            steps.append(ticks[0])
        assert "SEARCH runs USING" in plan, plan
    assert steps[0] == steps[1], steps


def test_without_exclusion_an_open_reader_blocks_a_journal_stop_commit(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[str] = []

    def writer_attempt(rows: list[Any]) -> list[Any]:
        with closing(CONNECT_ORIGINAL(live.journal, isolation_level=None, timeout=0)) as writer:
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE runs SET stop_requested=1")
                writer.execute("COMMIT")
                outcomes.append("committed")
            except sqlite3.OperationalError as error:
                outcomes.append(f"refused:{getattr(error, 'sqlite_errorcode', None)}")
                if writer.in_transaction:
                    writer.execute("ROLLBACK")
        return rows

    tape = Tape(monkeypatch, rows=(qs.RUN_QUERY, 1, writer_attempt))
    result = live.read()
    tape.dispose()

    assert outcomes == ["refused:5"], outcomes
    ok(result)
    with closing(CONNECT(live.journal)) as conn:
        assert conn.execute("SELECT stop_requested FROM runs").fetchall() == [(0,)]


@pytest.mark.parametrize("seq", [0, -1])
def test_the_capture_receipt_sequence_is_only_range_checked(live: Live, seq: int) -> None:
    """The retained inputs carry no sequence, so only its signed64 range can be checked."""
    with closing(CONNECT(live.ledger, isolation_level=None)) as conn:
        conn.execute("UPDATE receipts SET seq=? WHERE action='capture'", (seq,))
    result = live.read()
    no_association(result)
    assert result.refusal == qs.Refusal.ROW_REFUSED, result
