import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import effective_queue as queue
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import (
        EMPTY,
        create_fixture,
        queue_path,
        tables,
    )
    from packages.agent_board.tests.test_night_effective_queue_counts import TRANSITIONS, Operation, Prepare
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_effective_queue import EMPTY, create_fixture, queue_path, tables
    from test_night_effective_queue_counts import TRANSITIONS, Operation, Prepare
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
CASES = [
    pytest.param(name, prepare, operation, index, when, id=f"{name}-{index}-{when}")
    for name, count, prepare, operation in TRANSITIONS
    for index in range(count)
    for when in ("before", "after")
]


@pytest.mark.parametrize(("name", "prepare", "operation", "index", "when"), CASES)
def test_a_failure_at_any_statement_returns_no_authority_and_keeps_uncertainty(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    prepare: Prepare,
    operation: Operation,
    index: int,
    when: str,
) -> None:
    control = prepare(queue_path)
    before = tables(queue_path)
    tape = Tape(monkeypatch, fail_at=index, when=when)

    result = operation(queue_path, control)
    tape.dispose()

    statement = tape.attempts[index]
    assert result.control is None and result.selected is None
    expected = queue.Fault.ROLLBACK_FAILURE if statement == "ROLLBACK" else queue.Fault.SQLITE_FAILURE
    refusal = queue.Refusal.CONTROL_MISMATCH if name == "stale-control" and statement == "ROLLBACK" else None
    assert (result.fault, result.refusal) == (expected, refusal), result
    assert tape.closes == 1 and tape.attempts.count("ROLLBACK") <= 1
    assert result.cleanup == CleanupOutcome.CLOSED
    tape.reconcile()
    if statement == "COMMIT":
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        committed = tables(queue_path) != before
        assert committed is (when == "after")
    else:
        assert tables(queue_path) == before
        assert result.mutation in (MutationOutcome.NOT_ATTEMPTED, MutationOutcome.ROLLED_BACK)
        begun = "BEGIN IMMEDIATE" in tape.attempts[:index]
        assert result.mutation == (MutationOutcome.ROLLED_BACK if begun else MutationOutcome.NOT_ATTEMPTED)


COMPOUND = [
    pytest.param(
        name,
        prepare,
        operation,
        landed,
        rollback,
        close,
        id=f"{name}-{landed}-rollback-{rollback}-close-{close}",
    )
    for name, _, prepare, operation in TRANSITIONS
    if name.startswith(("publish", "acknowledge")) and name != "publish-identical"
    for landed in ("before", "after")
    for rollback in ("before", "after", None)
    for close in ("before", "after", None)
    if rollback or close
]


@pytest.mark.parametrize(("name", "prepare", "operation", "landed", "rollback", "close"), COMPOUND)
def test_a_commit_failure_compounded_with_cleanup_faults_stays_unknown_with_cleanup_first_precedence(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    prepare: Prepare,
    operation: Operation,
    landed: str,
    rollback: str | None,
    close: str | None,
) -> None:
    control = prepare(queue_path)
    probe = Tape(monkeypatch)
    operation(queue_path, control)
    probe.dispose()
    monkeypatch.undo()
    commit_at = probe.attempts.index("COMMIT")

    queue_path.unlink()
    create_fixture(queue_path)
    control = prepare(queue_path)
    before = tables(queue_path)
    tape = Tape(monkeypatch, fail_at=commit_at, when=landed, rollback_fault=rollback, close_fault=close)

    result = operation(queue_path, control)
    tape.dispose()

    assert (tables(queue_path) != before) is (landed == "after")

    assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
    assert result.control is None and result.selected is None
    rolled = landed == "before"
    expected = (
        queue.Fault.CLOSE_FAILURE
        if close
        else queue.Fault.ROLLBACK_FAILURE
        if rollback and rolled
        else queue.Fault.SQLITE_FAILURE
    )
    assert result.fault == expected
    assert result.cleanup == (CleanupOutcome.UNCERTAIN if close else CleanupOutcome.CLOSED)
    assert tape.attempts.count("ROLLBACK") == int(rolled)


@pytest.mark.parametrize("lock", ["EXCLUSIVE", "IMMEDIATE"])
def test_a_writer_holding_the_lock_is_a_busy_refusal_before_begin(queue_path: Path, lock: str) -> None:
    holder = sqlite3.connect(queue_path, isolation_level=None)
    try:
        holder.execute(f"BEGIN {lock}")
        result = queue.observe_ordinary(queue_path, expected=EMPTY)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert result.refusal == queue.Refusal.BUSY and result.fault is None, result
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.control is None


@pytest.mark.parametrize(
    ("name", "prepare", "operation"),
    [
        pytest.param(name, prepare, operation, id=name)
        for name, _, prepare, operation in TRANSITIONS
        if name.startswith(("publish", "acknowledge")) and name != "publish-identical"
    ],
)
def test_a_reader_blocking_the_real_commit_leaves_the_mutation_unknown(
    queue_path: Path, name: str, prepare: Prepare, operation: Operation
) -> None:
    control = prepare(queue_path)
    before = tables(queue_path)
    reader = sqlite3.connect(queue_path, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM receipts").fetchall()
        result = operation(queue_path, control)
    finally:
        reader.execute("ROLLBACK")
        reader.close()

    assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
    assert result.fault == queue.Fault.SQLITE_FAILURE and result.refusal is None
    assert result.control is None and result.selected is None
    assert tables(queue_path) == before
