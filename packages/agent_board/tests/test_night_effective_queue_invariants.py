import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import effective_queue as queue
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import CONNECT, queue_path, tables
    from packages.agent_board.tests.test_night_effective_queue_counts import TRANSITIONS
else:
    from test_night_effective_queue import CONNECT, queue_path, tables
    from test_night_effective_queue_counts import TRANSITIONS

__all__ = ["queue_path"]
MUTATING = {
    name: (prepare, operation)
    for name, _, prepare, operation in TRANSITIONS
    if name in ("publish-binding", "publish-evidence", "acknowledge-binding", "acknowledge-evidence")
}


class Harness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        after: str | None = None,
        injected: tuple[str, tuple[object, ...]] | None = None,
        rowcount: tuple[int, int] | None = None,
        lastrowid: int | None = None,
        read: tuple[str, str, int] | None = None,
        rollback: str | None = None,
        close: str | None = None,
        error: tuple[object, int | str] | None = None,
    ) -> None:
        self.attempts: list[str] = []
        self.native: list[str] = []
        self.skipped: list[int] = []
        self.connections: list[sqlite3.Connection] = []
        self.closes = 0
        harness = self
        writes = [0]
        reads: dict[str, int] = {}

        def failure() -> sqlite3.Error:
            assert error is not None
            raised = sqlite3.OperationalError("database is locked")
            if error[0] is not None:
                raised.sqlite_errorcode = error[0]  # type: ignore[assignment]
            return raised

        class Cursor(sqlite3.Cursor):
            statement = ""
            write = -1

            @property
            def rowcount(self) -> int:
                if rowcount is not None and self.write == rowcount[0]:
                    return rowcount[1]
                return super().rowcount

            @property
            def lastrowid(self) -> int | None:
                if lastrowid is not None and self.statement.startswith("INSERT INTO receipts"):
                    return lastrowid
                return super().lastrowid

            def fetchall(self) -> list[Any]:
                actual = super().fetchall()
                if read is None or self.statement != read[0]:
                    return actual
                reads[self.statement] = reads.get(self.statement, 0) + 1
                if reads[self.statement] != read[2]:
                    return actual
                return [] if read[1] == "empty" else actual * 2

        class Connection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                index = len(harness.attempts)
                harness.attempts.append(sql)
                if error is not None and (index == error[1] or sql == error[1]):
                    harness.skipped.append(index)
                    raise failure()
                if sql == "ROLLBACK" and rollback == "before":
                    harness.skipped.append(index)
                    raise sqlite3.OperationalError("injected rollback before native")
                cursor = self.cursor(factory=Cursor)
                cursor.statement = sql
                if sql.startswith(("INSERT", "UPDATE")):
                    cursor.write = writes[0]
                    writes[0] += 1
                cursor.execute(sql, parameters)
                if after is not None and sql.startswith(after) and injected is not None:
                    super().execute(*injected)
                if sql == "ROLLBACK" and rollback == "after":
                    raise sqlite3.OperationalError("injected rollback after native")
                return cursor

            def close(self) -> None:
                harness.closes += 1
                if close == "before":
                    raise sqlite3.OperationalError("injected close before native")
                super().close()
                if close == "after":
                    raise sqlite3.OperationalError("injected close after native")

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            assert kwargs == {"uri": True, "isolation_level": None, "timeout": 0}
            if error is not None and error[1] == "connect":
                raise failure()
            conn = CONNECT(*args, **kwargs, factory=Connection)
            conn.set_trace_callback(harness.native.append)
            harness.connections.append(conn)
            return conn

        monkeypatch.setattr(sqlite3, "connect", connect)

    def dispose(self) -> None:
        for conn in self.connections:
            sqlite3.Connection.close(conn)


OTHER_RECEIPT = "70000000-0000-4000-8000-000000000000"
CORRUPTIONS = [
    ("publish-binding", "INSERT INTO receipts", ("UPDATE bindings SET uid=1001", ())),
    ("publish-evidence", "INSERT INTO receipts", ("UPDATE bindings SET uid=1001", ())),
    ("publish-binding", "INSERT INTO receipts", ("UPDATE bindings SET invocation_id=?", ("b" * 32,))),
    (
        "acknowledge-binding",
        "UPDATE receipts",
        ("UPDATE receipts SET receipt_id=? WHERE seq=(SELECT max(seq) FROM receipts)", (OTHER_RECEIPT,)),
    ),
    (
        "acknowledge-evidence",
        "UPDATE receipts",
        ("UPDATE receipts SET receipt_id=? WHERE seq=(SELECT max(seq) FROM receipts)", (OTHER_RECEIPT,)),
    ),
    ("acknowledge-binding", "UPDATE receipts", ("UPDATE bindings SET uid=1001", ())),
    ("acknowledge-evidence", "UPDATE receipts", ("UPDATE bindings SET uid=1001", ())),
    (
        "acknowledge-evidence",
        "UPDATE receipts",
        (
            "UPDATE receipts SET payload=replace(payload,'empty','uncertain') WHERE seq=(SELECT max(seq) FROM receipts)",
            (),
        ),
    ),
]


@pytest.mark.parametrize(
    ("name", "after", "injected"),
    [pytest.param(*case, id=f"{case[0]}-{case[2][0].split(' SET ')[1][:14]}") for case in CORRUPTIONS],
)
def test_a_valid_but_wrong_field_written_during_the_transition_is_a_snapshot_failure(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    after: str,
    injected: tuple[str, tuple[object, ...]],
) -> None:
    prepare, operation = MUTATING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    harness = Harness(monkeypatch, after=after, injected=injected)

    result = operation(queue_path, control)
    harness.dispose()

    assert (result.fault, result.refusal) == (queue.Fault.SNAPSHOT_FAILURE, None), result
    assert (
        result.mutation == MutationOutcome.ROLLED_BACK and result.control is None and result.selected is None
    )
    assert "COMMIT" not in harness.attempts and harness.attempts.count("ROLLBACK") == 1
    assert tables(queue_path) == before


def _write_counts() -> list[tuple[str, int]]:
    counts = {
        "publish-binding": 3,
        "publish-evidence": 2,
        "acknowledge-binding": 4,
        "acknowledge-evidence": 3,
    }
    return [(name, index) for name, count in counts.items() for index in range(count)]


ANOMALIES = [
    pytest.param(name, {"rowcount": (index, value)}, id=f"{name}-write{index}-rowcount{value}")
    for name, index in _write_counts()
    for value in (0, 2, -1)
]
ANOMALIES += [
    pytest.param(name, {"read": (query, shape, number)}, id=f"{name}-post-{label}-{shape}")
    for name in MUTATING
    for query, label, number in (
        (queue.CONTROL_QUERY, "control", 2),
        (queue.SELECTED_QUERY, "selected", 1 if name.startswith("publish") else 2),
        (queue.BINDING_QUERY, "binding", 2),
    )
    for shape in ("empty", "duplicate")
]
ANOMALIES += [
    pytest.param(name, {"lastrowid": 99}, id=f"{name}-lastrowid")
    for name in ("publish-binding", "publish-evidence")
]


@pytest.mark.parametrize(("name", "anomaly"), ANOMALIES)
@pytest.mark.parametrize("rollback", [None, "before", "after"])
@pytest.mark.parametrize("close", [None, "before", "after"])
def test_an_impossible_write_or_read_result_is_a_snapshot_fault_with_cleanup_first_precedence(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    anomaly: dict[str, Any],
    rollback: str | None,
    close: str | None,
) -> None:
    prepare, operation = MUTATING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    harness = Harness(monkeypatch, rollback=rollback, close=close, **anomaly)

    result = operation(queue_path, control)
    harness.dispose()

    expected = (
        queue.Fault.CLOSE_FAILURE
        if close
        else queue.Fault.ROLLBACK_FAILURE
        if rollback
        else queue.Fault.SNAPSHOT_FAILURE
    )
    assert (result.fault, result.refusal) == (expected, None), result
    assert result.control is None and result.selected is None
    assert result.cleanup == (CleanupOutcome.UNCERTAIN if close else CleanupOutcome.CLOSED)
    uncertain = bool(close and rollback)
    assert result.mutation == (MutationOutcome.UNKNOWN if uncertain else MutationOutcome.ROLLED_BACK)
    assert "COMMIT" not in harness.attempts and harness.attempts.count("ROLLBACK") == 1 == harness.closes
    assert len(harness.native) + len(harness.skipped) == len(harness.attempts)
    assert tables(queue_path) == before


@pytest.mark.parametrize("code", [None, True, "5", 5, 6, 261, 262, 517])
@pytest.mark.parametrize("phase", ["connect", 0, 3, 4, "COMMIT"])
@pytest.mark.parametrize("name", ["publish-binding", "acknowledge-evidence"])
def test_only_exact_typed_busy_or_locked_before_begin_is_a_busy_refusal(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, code: Any, phase: str | int, name: str
) -> None:
    prepare, operation = MUTATING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    harness = Harness(monkeypatch, error=(code, phase))

    result = operation(queue_path, control)
    harness.dispose()

    busy = type(code) is int and code & 0xFF in (5, 6) and phase in ("connect", 0, 3)
    assert result.refusal == (queue.Refusal.BUSY if busy else None)
    assert result.fault == (None if busy else queue.Fault.SQLITE_FAILURE)
    assert result.mutation == (
        MutationOutcome.UNKNOWN
        if phase == "COMMIT"
        else MutationOutcome.ROLLED_BACK
        if phase == 4
        else MutationOutcome.NOT_ATTEMPTED
    )
    assert result.changed is (None if phase == "COMMIT" else False)
    assert result.cleanup == CleanupOutcome.CLOSED and result.control is None
    assert harness.closes == len(harness.connections) == (0 if phase == "connect" else 1)
    assert harness.attempts.count("ROLLBACK") == int(phase in (4, "COMMIT"))
    assert len(harness.native) + len(harness.skipped) == len(harness.attempts)
    for conn in harness.connections:
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(conn, "SELECT 1")
    assert tables(queue_path) == before
