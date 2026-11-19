import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import effective_queue as eq
from agent_board.night import queue_arm as qa
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import (
        CONNECT,
        queue_path,
        tables,
    )
    from packages.agent_board.tests.test_night_effective_queue_invariants import Harness
    from packages.agent_board.tests.test_night_effective_queue_scale import history
    from packages.agent_board.tests.test_night_ledger_arm import Tape
    from packages.agent_board.tests.test_night_queue_arm import ARM, LATER, TRANSITIONS, armed, publish
else:
    from test_night_effective_queue import CONNECT, queue_path, tables
    from test_night_effective_queue_invariants import Harness
    from test_night_effective_queue_scale import history
    from test_night_ledger_arm import Tape
    from test_night_queue_arm import ARM, LATER, TRANSITIONS, armed, publish

__all__ = ["queue_path"]
CHANGING = {
    name: (prepare, operation)
    for name, _, _, prepare, operation in TRANSITIONS
    if name in ("insert", "replace")
}
CASES = [
    pytest.param(name, prepare, operation, index, when, id=f"{name}-{index}-{when}")
    for name, count, _, prepare, operation in TRANSITIONS
    for index in range(count)
    for when in ("before", "after")
]


@pytest.mark.parametrize(("name", "prepare", "operation", "index", "when"), CASES)
def test_a_failure_at_any_statement_returns_no_control_or_stored_arm(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    prepare: Any,
    operation: Any,
    index: int,
    when: str,
) -> None:
    control = prepare(queue_path)
    before = tables(queue_path)
    tape = Tape(monkeypatch, fail_at=index, when=when)

    result = operation(queue_path, control)
    tape.dispose()

    statement = tape.attempts[index]
    assert result.control is None and result.stored is None
    assert result.fault == (qa.Fault.ROLLBACK_FAILURE if statement == "ROLLBACK" else qa.Fault.SQLITE_FAILURE)
    assert (
        tape.closes == 1 and tape.attempts.count("ROLLBACK") <= 1 and result.cleanup == CleanupOutcome.CLOSED
    )
    tape.reconcile()
    if statement == "COMMIT":
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        assert (tables(queue_path) != before) is (when == "after")
    else:
        assert tables(queue_path) == before
        begun = "BEGIN IMMEDIATE" in tape.attempts[:index]
        assert result.mutation == (MutationOutcome.ROLLED_BACK if begun else MutationOutcome.NOT_ATTEMPTED)


@pytest.mark.parametrize(
    ("name", "landed", "rollback", "close"),
    [
        pytest.param(name, landed, rollback, close, id=f"{name}-{landed}-{rollback}-{close}")
        for name in CHANGING
        for landed in ("before", "after")
        for rollback in ("before", "after", None)
        for close in ("before", "after", None)
        if rollback or close
    ],
)
def test_a_commit_exception_is_unknown_with_cleanup_first_precedence(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    landed: str,
    rollback: str | None,
    close: str | None,
) -> None:
    prepare, operation = CHANGING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    tape = Tape(monkeypatch, fail_at=15, when=landed, rollback_fault=rollback, close_fault=close)

    result = operation(queue_path, control)
    tape.dispose()

    assert tape.attempts[15] == "COMMIT"
    assert (tables(queue_path) != before) is (landed == "after")
    assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
    assert result.control is None and result.stored is None
    rolled = landed == "before"
    expected = (
        qa.Fault.CLOSE_FAILURE
        if close
        else qa.Fault.ROLLBACK_FAILURE
        if rollback and rolled
        else qa.Fault.SQLITE_FAILURE
    )
    assert result.fault == expected and tape.attempts.count("ROLLBACK") == int(rolled)
    assert result.cleanup == (CleanupOutcome.UNCERTAIN if close else CleanupOutcome.CLOSED)


def changed(name: str, value: Any) -> Any:
    if isinstance(value, bytes):
        return (b"b" if value[:1] == b"a" else b"a") + value[1:]
    if isinstance(value, float):
        return value + 1.0
    assert isinstance(value, int)
    return value + 1


POST_READS = [
    (qa.ARM_QUERY, 2, list(qa._ARM)),
    (eq.IDENTITY_QUERY, 2, list(eq._IDENTITY)),
    (eq.CONTROL_QUERY, 2, list(eq._CONTROL)),
]


@pytest.mark.parametrize(
    ("name", "query", "number", "column"),
    [
        pytest.param(name, query, number, column, id=f"{name}-{query.split(' FROM ')[1].split()[0]}-{column}")
        for name in CHANGING
        for query, number, columns in POST_READS
        for column in columns
    ],
)
def test_every_column_of_every_post_write_read_is_compared(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, query: str, number: int, column: str
) -> None:
    prepare, operation = CHANGING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    reads = [0]
    mutated: list[str] = []

    class Cursor:
        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self.cursor = cursor

        def __getattr__(self, attribute: str) -> Any:
            return getattr(self.cursor, attribute)

        def fetchall(self) -> list[Any]:
            rows = self.cursor.fetchall()
            reads[0] += 1
            if reads[0] != number or len(rows) != 1:
                return rows
            row = dict(rows[0])
            if row[column] is None:
                row[column], row[f"{column}_kind"] = 1, "integer"
            else:
                row[column] = changed(column, row[column])
            mutated.append(column)
            return [row]

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> Any:
            cursor = super().execute(sql, parameters)
            return Cursor(cursor) if sql == query else cursor

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return CONNECT(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    result = operation(queue_path, control)
    monkeypatch.undo()

    assert mutated == [column]
    assert (result.fault, result.refusal) == (qa.Fault.SNAPSHOT_FAILURE, None), result
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.control is None and result.stored is None
    assert tables(queue_path) == before


@pytest.mark.parametrize(
    ("name", "index", "value"), [(n, i, v) for n in CHANGING for i in (0, 1) for v in (0, 2, -1)]
)
@pytest.mark.parametrize("rollback", [None, "before", "after"])
@pytest.mark.parametrize("close", [None, "before", "after"])
def test_an_impossible_rowcount_is_a_snapshot_fault_with_cleanup_first_precedence(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    index: int,
    value: int,
    rollback: str | None,
    close: str | None,
) -> None:
    prepare, operation = CHANGING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    harness = Harness(monkeypatch, rowcount=(index, value), rollback=rollback, close=close)

    result = operation(queue_path, control)
    harness.dispose()

    expected = (
        qa.Fault.CLOSE_FAILURE
        if close
        else qa.Fault.ROLLBACK_FAILURE
        if rollback
        else qa.Fault.SNAPSHOT_FAILURE
    )
    assert (result.fault, result.refusal) == (expected, None), result
    assert result.control is None and result.stored is None
    assert result.mutation == (MutationOutcome.UNKNOWN if close and rollback else MutationOutcome.ROLLED_BACK)
    assert "COMMIT" not in harness.attempts and harness.attempts.count("ROLLBACK") == 1 == harness.closes
    assert tables(queue_path) == before


@pytest.mark.parametrize("code", [None, True, "5", 5, 6, 261, 262, 517])
@pytest.mark.parametrize("phase", ["connect", 0, 3, 4, "COMMIT"])
def test_only_exact_typed_busy_or_locked_before_begin_is_a_busy_refusal(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, code: Any, phase: str | int
) -> None:
    control = armed(queue_path)
    before = tables(queue_path)
    harness = Harness(monkeypatch, error=(code, phase))

    result = publish(queue_path, control, LATER, ARM)
    harness.dispose()

    busy = type(code) is int and code & 0xFF in (5, 6) and phase in ("connect", 0, 3)
    assert result.refusal == (qa.Refusal.BUSY if busy else None)
    assert result.fault == (None if busy else qa.Fault.SQLITE_FAILURE)
    assert result.mutation == (
        MutationOutcome.UNKNOWN
        if phase == "COMMIT"
        else MutationOutcome.ROLLED_BACK
        if phase == 4
        else MutationOutcome.NOT_ATTEMPTED
    )
    assert result.cleanup == CleanupOutcome.CLOSED and result.control is None
    assert harness.closes == len(harness.connections) == (0 if phase == "connect" else 1)
    assert tables(queue_path) == before


def unrelated_arms(path: Path, size: int, start: int = 0) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO arms VALUES(?,?,?,?,?)",
            (
                (f"{n:08x}-0000-4000-8000-{n:012x}", ARM.guard_boot_id, "e" * 32, 10.0, 20.0)
                for n in range(start, start + size)
            ),
        )
        conn.execute("COMMIT")


@pytest.mark.parametrize("size", [10, 10_000, 100_000])
@pytest.mark.parametrize(
    ("count", "prepare", "operation"),
    [pytest.param(count, prepare, operation, id=name) for name, count, _, prepare, operation in TRANSITIONS],
)
def test_schedules_do_not_grow_with_unrelated_arms_and_receipts(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, count: int, prepare: Any, operation: Any
) -> None:
    history(queue_path, size)
    unrelated_arms(queue_path, size)
    control = prepare(queue_path)
    before = tables(queue_path)
    tape = Tape(monkeypatch)

    result = operation(queue_path, control)
    tape.dispose()

    assert result.fault is None and len(tape.native) == len(tape.attempts) == count
    after = tables(queue_path)
    assert [row for row in after["arms"] if row[0] != ARM.run_id] == [
        row for row in before["arms"] if row[0] != ARM.run_id
    ]
    assert after["receipts"] == before["receipts"] and after["bindings"] == before["bindings"]


def test_the_arm_lookup_is_a_key_search_with_constant_cost(queue_path: Path) -> None:
    ticks = [0]

    def tick() -> int:
        ticks[0] += 1
        return 0

    steps: list[int] = []
    for size, start in ((10, 0), (10_000, 10)):
        unrelated_arms(queue_path, size - start, start)
        with closing(sqlite3.connect(queue_path)) as conn:
            plan = " ".join(
                row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {qa.ARM_QUERY}", (ARM.run_id,))
            )
            ticks[0] = 0
            conn.set_progress_handler(tick, 1)
            conn.execute(qa.ARM_QUERY, (ARM.run_id,)).fetchall()
            steps.append(ticks[0])
        assert "SEARCH arms USING INDEX sqlite_autoindex_arms_1" in plan, plan
    assert steps[0] == steps[1], steps
