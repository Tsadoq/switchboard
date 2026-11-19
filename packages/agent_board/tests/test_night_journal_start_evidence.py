import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import journal_start as js
from agent_board.night import queue_association as qs
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.journal_start import Fault, Refusal, StartClaimResult

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_journal_start import (
        BOOT,
        FENCE,
        GRANT,
        PRINCIPAL,
        RUN,
        UID,
        claim,
        legacy,
        result,
        rows,
    )
else:
    from test_night_journal_start import BOOT, FENCE, GRANT, PRINCIPAL, RUN, UID, claim, legacy, result, rows

CONNECT_ORIGINAL = sqlite3.connect
N, C, RB, U = (
    MutationOutcome.NOT_ATTEMPTED,
    MutationOutcome.COMMITTED,
    MutationOutcome.ROLLED_BACK,
    MutationOutcome.UNKNOWN,
)
CLOSED, UNCERTAIN = CleanupOutcome.CLOSED, CleanupOutcome.UNCERTAIN
SCHEDULE = [
    *js.SETUP,
    "PRAGMA journal_mode",
    "PRAGMA user_version",
    qs.JOURNAL_CATALOG_QUERY,
    qs.RUN_QUERY,
    qs.ACTIVE_QUERY,
    js.INSERT,
    qs.RUN_QUERY,
    qs.ACTIVE_QUERY,
    "COMMIT",
]
INSERT_AT, COMMIT_AT = SCHEDULE.index(js.INSERT), len(SCHEDULE) - 1


class Tape:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail_at: int | None = None,
        when: str = "before",
        code: object = None,
        rollback: str | None = None,
        close: str | None = None,
        rows_at: tuple[int, Callable[[list[Any]], list[Any]]] | None = None,
        rowcount: int | None = None,
        interrupt_at: int | None = None,
    ) -> None:
        self.attempts: list[str] = []
        self.native: list[str] = []
        self.connections: list[sqlite3.Connection] = []
        tape = self

        def failure(message: str) -> sqlite3.Error:
            raised = sqlite3.OperationalError(message)
            if code is not None:
                raised.sqlite_errorcode = code  # type: ignore[assignment]
            return raised

        class Cursor:
            def __init__(self, cursor: sqlite3.Cursor, index: int) -> None:
                self.cursor, self.index = cursor, index

            def __getattr__(self, name: str) -> Any:
                if name == "rowcount" and rowcount is not None:
                    return rowcount
                return getattr(self.cursor, name)

            def fetchall(self) -> list[Any]:
                fetched = self.cursor.fetchall()
                if rows_at is not None and rows_at[0] == self.index:
                    return rows_at[1](fetched)
                return fetched

        class Connection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> Any:
                index = len(tape.attempts)
                tape.attempts.append(sql)
                if index == interrupt_at:
                    raise KeyboardInterrupt
                fault = when if index == fail_at else None
                if sql == "ROLLBACK" and rollback is not None:
                    fault = rollback
                if fault == "before":
                    raise failure("injected before execute")
                cursor = super().execute(sql, parameters)
                if fault == "after":
                    raise failure("injected after execute")
                return Cursor(cursor, index)

            def close(self) -> None:
                if close == "before":
                    raise sqlite3.OperationalError("injected before close")
                super().close()
                if close == "after":
                    raise sqlite3.OperationalError("injected after close")

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            assert kwargs == {"uri": True, "isolation_level": None, "timeout": 0}
            assert args[0].endswith("?mode=rw")
            conn = CONNECT_ORIGINAL(*args, **kwargs, factory=Connection)
            conn.set_trace_callback(tape.native.append)
            tape.connections.append(conn)
            return conn

        monkeypatch.setattr(sqlite3, "connect", connect)

    def dispose(self) -> None:
        for conn in self.connections:
            sqlite3.Connection.close(conn)


@pytest.fixture
def journal() -> Iterator[Path]:
    with TemporaryDirectory(prefix="jse.", dir=Path.home()) as directory:
        path = Path(directory) / "state" / "journal.sqlite3"
        legacy(path)
        yield path


def path_descriptors(path: Path) -> list[int]:
    owned: list[int] = []
    names = {str(path), *(f"{path}{suffix}" for suffix in ("-journal", "-wal", "-shm"))}
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{entry}")
        except FileNotFoundError:
            continue
        if target in names:
            owned.append(int(entry))
    return owned


def assert_released(path: Path, tape: Tape) -> None:
    assert path_descriptors(path) == []
    assert tape.connections
    for conn in tape.connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(conn, "SELECT 1")


def test_normal_schedule_is_thirteen_statements(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tape = Tape(monkeypatch)
    assert claim(journal).usable
    assert tape.attempts == SCHEDULE
    assert len(tape.attempts) == 13
    assert len(tape.native) == 13


def _expected(index: int, when: str) -> StartClaimResult:
    statement = SCHEDULE[index]
    if index < INSERT_AT:
        return result(N, fault=Fault.SQLITE_FAILURE)
    if statement == "COMMIT" and when == "after":
        return result(U, UNCERTAIN, fault=Fault.ROLLBACK_FAILURE)
    if statement == "COMMIT":
        return result(U, fault=Fault.SQLITE_FAILURE)
    return result(RB, fault=Fault.SQLITE_FAILURE)


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("index", range(13))
def test_fault_before_and_after_every_statement(
    journal: Path, monkeypatch: pytest.MonkeyPatch, index: int, when: str
) -> None:
    assert path_descriptors(journal) == []
    tape = Tape(monkeypatch, fail_at=index, when=when)
    outcome = claim(journal)
    assert outcome == _expected(index, when)
    assert len(tape.attempts) <= 14
    begun = index > 3 or (index == 3 and when == "after")
    rollback = ["ROLLBACK"] if begun else []
    assert tape.attempts == SCHEDULE[: index + 1] + rollback
    assert len(tape.native) == len(tape.attempts) - (1 if when == "before" else 0)
    assert_released(journal, tape)
    if SCHEDULE[index] != "COMMIT":
        assert rows(journal) == []


@pytest.mark.parametrize(
    ("index", "when", "code", "expected"),
    [
        (0, "before", 5, Refusal.BUSY),
        (0, "before", 6, Refusal.BUSY),
        (3, "before", 5, Refusal.BUSY),
        (3, "before", 6, Refusal.BUSY),
        (3, "before", 5 | (1 << 8), Refusal.BUSY),
        (3, "before", 6 | (1 << 8), Refusal.BUSY),
        (3, "before", 5 | (2 << 8), Refusal.BUSY),
        (3, "before", -5, Fault.SQLITE_FAILURE),
        (3, "before", True, Fault.SQLITE_FAILURE),
        (3, "before", "5", Fault.SQLITE_FAILURE),
        (3, "before", 5.0, Fault.SQLITE_FAILURE),
        (3, "before", 1, Fault.SQLITE_FAILURE),
        (3, "after", 5, Fault.SQLITE_FAILURE),
        (4, "before", 5, Fault.SQLITE_FAILURE),
        (COMMIT_AT, "before", 5, Fault.SQLITE_FAILURE),
    ],
)
def test_typed_busy_only_before_any_begin(
    journal: Path, monkeypatch: pytest.MonkeyPatch, index: int, when: str, code: object, expected: object
) -> None:
    Tape(monkeypatch, fail_at=index, when=when, code=code)
    outcome = claim(journal)
    if expected is Refusal.BUSY:
        assert outcome == result(N, refusal=Refusal.BUSY)
    else:
        assert outcome.refusal is None and outcome.fault is Fault.SQLITE_FAILURE


def _active_row(fetched: list[Any]) -> list[Any]:
    return [{"run_id": b"2000000c-0000-4000-8000-00000000000f", "run_id_kind": "text"}]


@pytest.mark.parametrize("rollback", ["before", "after"])
def test_prewrite_refusal_with_rollback_failure(
    journal: Path, monkeypatch: pytest.MonkeyPatch, rollback: str
) -> None:
    Tape(monkeypatch, rows_at=(SCHEDULE.index(qs.ACTIVE_QUERY), _active_row), rollback=rollback)
    assert claim(journal) == result(N, UNCERTAIN, Refusal.ACTIVE_PRESENT, Fault.ROLLBACK_FAILURE)


@pytest.mark.parametrize(("rollback", "close"), [(None, "before"), (None, "after"), ("before", "after")])
def test_prewrite_refusal_with_close_failure(
    journal: Path, monkeypatch: pytest.MonkeyPatch, rollback: str | None, close: str
) -> None:
    tape = Tape(
        monkeypatch, rows_at=(SCHEDULE.index(qs.ACTIVE_QUERY), _active_row), rollback=rollback, close=close
    )
    try:
        assert claim(journal) == result(N, UNCERTAIN, Refusal.ACTIVE_PRESENT, Fault.CLOSE_FAILURE)
    finally:
        tape.dispose()


@pytest.mark.parametrize(
    ("rollback", "close", "expected"),
    [
        (None, None, result(RB, fault=Fault.SQLITE_FAILURE)),
        ("before", None, result(U, UNCERTAIN, fault=Fault.ROLLBACK_FAILURE)),
        ("after", None, result(U, UNCERTAIN, fault=Fault.ROLLBACK_FAILURE)),
        (None, "after", result(RB, UNCERTAIN, fault=Fault.CLOSE_FAILURE)),
        ("before", "after", result(U, UNCERTAIN, fault=Fault.CLOSE_FAILURE)),
        ("after", "before", result(U, UNCERTAIN, fault=Fault.CLOSE_FAILURE)),
    ],
)
def test_post_dml_cleanup_compounds(
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback: str | None,
    close: str | None,
    expected: StartClaimResult,
) -> None:
    tape = Tape(monkeypatch, fail_at=INSERT_AT + 1, rollback=rollback, close=close)
    try:
        assert claim(journal) == expected
    finally:
        tape.dispose()


@pytest.mark.parametrize("close", ["before", "after"])
def test_commit_then_close_failure_is_committed_but_unusable(
    journal: Path, monkeypatch: pytest.MonkeyPatch, close: str
) -> None:
    tape = Tape(monkeypatch, close=close)
    try:
        outcome = claim(journal)
    finally:
        tape.dispose()
    assert outcome == result(C, UNCERTAIN, fault=Fault.CLOSE_FAILURE)
    assert not outcome.usable
    assert "ROLLBACK" not in tape.attempts


@pytest.mark.parametrize(("rollback", "close"), [("before", None), (None, "after"), ("after", "before")])
def test_commit_exception_stays_unknown_whatever_cleanup_does(
    journal: Path, monkeypatch: pytest.MonkeyPatch, rollback: str | None, close: str | None
) -> None:
    tape = Tape(monkeypatch, fail_at=COMMIT_AT, rollback=rollback, close=close)
    try:
        outcome = claim(journal)
    finally:
        tape.dispose()
    assert outcome.mutation is U and outcome.cleanup is UNCERTAIN
    assert outcome.fault is (Fault.CLOSE_FAILURE if close else Fault.ROLLBACK_FAILURE)
    assert tape.attempts.count("ROLLBACK") == 1
    assert len(tape.attempts) == 14


def test_bad_insert_rowcount_is_snapshot_failure(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Tape(monkeypatch, rowcount=0)
    assert claim(journal) == result(RB, fault=Fault.SNAPSHOT_FAILURE)
    assert rows(journal) == []


COLUMNS = list(qs._RUN)


def _altered(column: str, value: object) -> Callable[[list[Any]], list[Any]]:
    def change(fetched: list[Any]) -> list[Any]:
        row = dict(zip(names := fetched[0].keys(), (fetched[0][key] for key in names), strict=True))
        row[column] = value
        return [row]

    return change


ALTERED = {
    "run_id": b"2000000c-0000-4000-8000-00000000000f",
    "fence": b"G" * 43,
    "principal": b"po@huginn",
    "uid": UID + 1,
    "boot_id": b"3000000a-0000-4000-8000-00000000000c",
    "deadline_boottime": 701.0,
    "deadline_utc": 1601.0,
    "state": b"reserved",
    "launch_claimed": 0,
    "stop_requested": 1,
    "uncertain": 1,
    "reason": b"never_claimed",
    "slot": 2,
}


@pytest.mark.parametrize("column", COLUMNS)
def test_every_postwrite_column_mismatch_is_snapshot_failure(
    journal: Path, monkeypatch: pytest.MonkeyPatch, column: str
) -> None:
    value = ALTERED[column]
    kind = {"reason": "text"}.get(column)
    tape_rows = _altered(column, value)
    if kind:

        def tape_rows(fetched: list[Any]) -> list[Any]:
            row = dict(zip(names := fetched[0].keys(), (fetched[0][key] for key in names), strict=True))
            row[column], row[f"{column}_kind"] = value, kind
            return [row]

    Tape(monkeypatch, rows_at=(INSERT_AT + 1, tape_rows))
    assert claim(journal) == result(RB, fault=Fault.SNAPSHOT_FAILURE)
    assert rows(journal) == []


@pytest.mark.parametrize("column", COLUMNS)
def test_every_postwrite_column_type_change_is_snapshot_failure(
    journal: Path, monkeypatch: pytest.MonkeyPatch, column: str
) -> None:
    def change(fetched: list[Any]) -> list[Any]:
        row = dict(zip(names := fetched[0].keys(), (fetched[0][key] for key in names), strict=True))
        row[f"{column}_kind"] = "blob"
        return [row]

    Tape(monkeypatch, rows_at=(INSERT_AT + 1, change))
    assert claim(journal) == result(RB, fault=Fault.SNAPSHOT_FAILURE)


@pytest.mark.parametrize(
    ("offset", "change"),
    [
        (1, lambda fetched: []),
        (1, lambda fetched: fetched + fetched),
        (2, lambda fetched: []),
        (2, lambda fetched: fetched + fetched),
        (2, _active_row),
        (2, lambda fetched: [{"run_id": fetched[0]["run_id"], "run_id_kind": "blob"}]),
        (2, lambda fetched: [{"run_id": fetched[0]["run_id"].upper(), "run_id_kind": "text"}]),
    ],
    ids=[
        "named-none",
        "named-two",
        "active-none",
        "active-two",
        "active-other",
        "active-kind",
        "active-case",
    ],
)
def test_postwrite_cardinality_and_active_mismatch(
    journal: Path, monkeypatch: pytest.MonkeyPatch, offset: int, change: Callable[[list[Any]], list[Any]]
) -> None:
    Tape(monkeypatch, rows_at=(INSERT_AT + offset, change))
    assert claim(journal) == result(RB, fault=Fault.SNAPSHOT_FAILURE)


@pytest.mark.parametrize(
    ("index", "change", "expected"),
    [
        (SCHEDULE.index(qs.RUN_QUERY), lambda fetched: [object(), object()], Refusal.ROW_REFUSED),
        (SCHEDULE.index(qs.ACTIVE_QUERY), lambda fetched: [object(), object()], Refusal.ROW_REFUSED),
        (
            SCHEDULE.index(qs.ACTIVE_QUERY),
            lambda fetched: [{"run_id": b"not-a-uuid", "run_id_kind": "text"}],
            Refusal.ROW_REFUSED,
        ),
        (
            SCHEDULE.index(qs.ACTIVE_QUERY),
            lambda fetched: [{"run_id": b"\xff" * 36, "run_id_kind": "text"}],
            Refusal.ROW_REFUSED,
        ),
        (SCHEDULE.index(qs.ACTIVE_QUERY), _active_row, Refusal.ACTIVE_PRESENT),
        (SCHEDULE.index("PRAGMA journal_mode"), lambda fetched: [("wal",)], Refusal.SCHEMA_REFUSED),
        (SCHEDULE.index("PRAGMA user_version"), lambda fetched: [("2",)], Refusal.SCHEMA_REFUSED),
        (SCHEDULE.index("PRAGMA user_version"), lambda fetched: [], Refusal.SCHEMA_REFUSED),
        (SCHEDULE.index(qs.JOURNAL_CATALOG_QUERY), lambda fetched: fetched[:2], Refusal.SCHEMA_REFUSED),
        (
            SCHEDULE.index(qs.JOURNAL_CATALOG_QUERY),
            lambda fetched: fetched[:2] + fetched[:1],
            Refusal.SCHEMA_REFUSED,
        ),
    ],
)
def test_prewrite_row_and_schema_refusals(
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: int,
    change: Callable[[list[Any]], list[Any]],
    expected: Refusal,
) -> None:
    tape = Tape(monkeypatch, rows_at=(index, change))
    assert claim(journal) == result(N, refusal=expected)
    assert tape.attempts == SCHEDULE[: index + 1] + ["ROLLBACK"]


def _history(path: Path, count: int, *, active: bool) -> None:
    with closing(CONNECT_ORIGINAL(path, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, 1.0, 2.0, 'stopped', 1, 1, 0, 'server_settled', 1)",
            (
                (f"{index:08x}-0000-4000-8000-000000000000", FENCE, PRINCIPAL, UID, BOOT)
                for index in range(1, count + 1)
            ),
        )
        if active:
            conn.execute(
                "INSERT INTO runs VALUES ('ffffffff-0000-4000-8000-000000000000', ?, ?, ?, ?, "
                "700.0, 1600.0, 'starting', 1, 0, 0, NULL, 1)",
                (FENCE, PRINCIPAL, UID, BOOT),
            )
        conn.execute("COMMIT")


def _steps(path: Path, statement: str, parameters: tuple[object, ...]) -> tuple[int, list[str]]:
    counted = [0]
    with closing(CONNECT_ORIGINAL(f"{path.as_uri()}?mode=ro", uri=True)) as conn:

        def tick() -> int:
            counted[0] += 1
            return 0

        plan = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {statement}", parameters)]
        conn.set_progress_handler(tick, 1)
        conn.execute(statement, parameters).fetchall()
        conn.set_progress_handler(None, 1)
    return counted[0], plan


def test_history_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    samples: dict[int, dict[str, int]] = {}
    with TemporaryDirectory(prefix="jsh.", dir=Path.home()) as directory:
        for count in (10, 10_000, 100_000):
            measured: dict[str, int] = {}
            for active in (False, True):
                path = Path(directory) / f"{count}-{active}" / "journal.sqlite3"
                legacy(path)
                _history(path, count, active=active)
                for key, statement, parameters, index in (
                    (
                        "hit",
                        qs.RUN_QUERY,
                        ("00000001-0000-4000-8000-000000000000",),
                        "sqlite_autoindex_runs_1",
                    ),
                    ("miss", qs.RUN_QUERY, (RUN,), "sqlite_autoindex_runs_1"),
                    ("active", qs.ACTIVE_QUERY, (), "one_active_run"),
                ):
                    steps, plan = _steps(path, statement, parameters)
                    assert len(plan) == 1 and plan[0].startswith("SEARCH runs USING") and index in plan[0]
                    measured[f"{key}-{active}"] = steps
                with monkeypatch.context() as patch:
                    tape = Tape(patch)
                    outcome = claim(path)
                if active:
                    assert outcome == result(N, refusal=Refusal.ACTIVE_PRESENT)
                    assert len(tape.attempts) == 10
                else:
                    assert outcome.usable
                    assert tape.attempts == SCHEDULE
            samples[count] = measured
    assert samples[10] == samples[10_000] == samples[100_000]


def test_fields_are_owned_scalars_not_caller_objects(journal: Path) -> None:
    assert claim(journal).usable
    stored = rows(journal)[0]
    assert stored[:7] == (RUN, FENCE, PRINCIPAL, UID, BOOT, 700.0, 1600.0)
    assert GRANT.deadline_utc == 1600.0


@pytest.mark.parametrize("index", [3, INSERT_AT, COMMIT_AT])
def test_interrupt_propagates_after_bounded_cleanup(
    journal: Path, monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    assert path_descriptors(journal) == []
    tape = Tape(monkeypatch, interrupt_at=index)
    with pytest.raises(KeyboardInterrupt):
        claim(journal)
    assert (tape.attempts[-1] == "ROLLBACK") == (index > 3)
    assert_released(journal, tape)
    assert rows(journal) == []


def test_unrelated_descriptor_reclaimed_during_claim_does_not_matter(
    journal: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
    tape = Tape(monkeypatch)
    original = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        os.close(unrelated)
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    before = len(os.listdir("/proc/self/fd"))
    assert claim(journal).usable
    assert len(os.listdir("/proc/self/fd")) == before - 1
    assert_released(journal, tape)


def test_path_scoped_check_detects_a_real_leak(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    leaked: list[int] = []
    tape = Tape(monkeypatch)
    original = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        leaked.append(os.open(journal, os.O_RDONLY | os.O_CLOEXEC))
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        assert claim(journal).usable
        assert path_descriptors(journal) == leaked
        with pytest.raises(AssertionError):
            assert_released(journal, tape)
    finally:
        for fd in leaked:
            os.close(fd)
    assert_released(journal, tape)


def test_unclosed_connection_is_detected(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tape = Tape(monkeypatch, close="before")
    try:
        assert claim(journal).fault is Fault.CLOSE_FAILURE
        assert path_descriptors(journal) != []
        with pytest.raises(AssertionError):
            assert_released(journal, tape)
    finally:
        tape.dispose()
    assert_released(journal, tape)
