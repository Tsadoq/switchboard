import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue_counts import TRANSITIONS, Operation, Prepare
    from packages.agent_board.tests.test_night_effective_queue import create_fixture, queue_path, tables
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_capture_queue_counts import TRANSITIONS, Operation, Prepare
    from test_night_effective_queue import create_fixture, queue_path, tables
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
MUTATING = [
    (name, prepare, operation)
    for name, _, _, prepare, operation in TRANSITIONS
    if name.startswith(("publish-", "acknowledge-", "resolve-"))
]
CASES = [
    pytest.param(name, prepare, operation, index, when, id=f"{name}-{index}-{when}")
    for name, count, _, prepare, operation in TRANSITIONS
    for index in range(count)
    for when in ("before", "after")
]


@pytest.mark.parametrize(("name", "prepare", "operation", "index", "when"), CASES)
def test_a_failure_at_any_statement_returns_no_episode_completion_or_control(
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
    assert result.control is None and result.episode is None and result.completion is None
    expected = cq.Fault.ROLLBACK_FAILURE if statement == "ROLLBACK" else cq.Fault.SQLITE_FAILURE
    assert (result.fault, result.refusal) == (expected, None), result
    assert tape.closes == 1 and tape.attempts.count("ROLLBACK") <= 1
    assert result.cleanup == CleanupOutcome.CLOSED
    tape.reconcile()
    if statement == "COMMIT":
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        assert (tables(queue_path) != before) is (when == "after")
    else:
        assert tables(queue_path) == before
        begun = "BEGIN IMMEDIATE" in tape.attempts[:index]
        assert result.mutation == (MutationOutcome.ROLLED_BACK if begun else MutationOutcome.NOT_ATTEMPTED)


@pytest.mark.parametrize(
    ("name", "prepare", "operation", "landed", "rollback", "close"),
    [
        pytest.param(
            name, prepare, operation, landed, rollback, close, id=f"{name}-{landed}-{rollback}-{close}"
        )
        for name, prepare, operation in MUTATING
        for landed in ("before", "after")
        for rollback in ("before", "after", None)
        for close in ("before", "after", None)
        if rollback or close
    ],
)
def test_a_commit_exception_with_cleanup_faults_is_always_unknown_with_cleanup_first_precedence(
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
    assert result.control is None and result.episode is None and result.completion is None
    rolled = landed == "before"
    expected = (
        cq.Fault.CLOSE_FAILURE
        if close
        else cq.Fault.ROLLBACK_FAILURE
        if rollback and rolled
        else cq.Fault.SQLITE_FAILURE
    )
    assert result.fault == expected
    assert result.cleanup == (CleanupOutcome.UNCERTAIN if close else CleanupOutcome.CLOSED)
    assert tape.attempts.count("ROLLBACK") == int(rolled)


@pytest.mark.parametrize(
    ("name", "prepare", "operation"), [pytest.param(*case, id=case[0]) for case in MUTATING]
)
def test_a_reader_blocking_the_real_commit_leaves_it_unknown(
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
    assert result.mutation == MutationOutcome.UNKNOWN and result.fault == cq.Fault.SQLITE_FAILURE
    assert result.completion is None and result.control is None
    assert tables(queue_path) == before
