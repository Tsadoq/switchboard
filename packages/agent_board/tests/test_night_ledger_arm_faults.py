from dataclasses import astuple
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import ledger_arm as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import ARM, JOURNAL, Tape, ledger_path, put, read
else:
    from test_night_ledger_arm import ARM, JOURNAL, Tape, ledger_path, put, read

__all__ = ["ledger_path"]


@pytest.mark.parametrize("branch", ["insert", "replace"])
@pytest.mark.parametrize("stage", range(12))
@pytest.mark.parametrize("when", ["before", "after"])
def test_each_real_api_changed_statement_fault_reconciles_native_trace_and_transaction_truth(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    stage: int,
    when: str,
) -> None:
    from dataclasses import replace

    prior = ARM if branch == "replace" else None
    desired = replace(ARM, guard_invocation_id="b" * 32)
    if prior is not None:
        put(ledger_path, prior)
    tape = Tape(monkeypatch, fail_at=stage, when=when)
    try:
        result = writer.compare_and_set_arm(
            ledger_path, journal_id=JOURNAL, desired=desired, expected_prior=prior
        )
        assert len(tape.injected) == 1 and result.fault == writer.Fault.SQLITE_FAILURE
        assert result.stored is None and result.refusal is None and result.cleanup == CleanupOutcome.CLOSED
        assert tape.closes == 1 and tape.attempts.count("ROLLBACK") <= 1
        assert len(tape.attempts) <= (13 if stage == 11 else 12)
        tape.reconcile()
        if stage < 3 or (stage == 3 and when == "before"):
            assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
        elif stage == 3:
            assert result.mutation == MutationOutcome.NOT_ATTEMPTED
            assert tape.attempts[-1] == "ROLLBACK"
        elif stage == 11:
            assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        else:
            assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
        durable = desired if stage == 11 and when == "after" else prior
        assert read(ledger_path) == ([] if durable is None else [astuple(durable)])
    finally:
        tape.dispose()


@pytest.mark.parametrize("stage", range(10))
@pytest.mark.parametrize("when", ["before", "after"])
def test_every_unchanged_statement_fault_never_manufactures_completion_or_commits(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: int,
    when: str,
) -> None:
    put(ledger_path, ARM)
    tape = Tape(monkeypatch, fail_at=stage, when=when)
    try:
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=ARM)
        assert len(tape.injected) == 1 and result.stored is None and result.refusal is None
        assert result.fault == (writer.Fault.ROLLBACK_FAILURE if stage == 9 else writer.Fault.SQLITE_FAILURE)
        assert result.cleanup == CleanupOutcome.CLOSED and result.changed is False
        assert result.mutation == (
            MutationOutcome.NOT_ATTEMPTED if stage <= 3 else MutationOutcome.ROLLED_BACK
        )
        assert "COMMIT" not in tape.attempts and tape.attempts.count("ROLLBACK") <= 1
        assert len(tape.attempts) <= 10 and tape.closes == 1
        tape.reconcile()
        assert read(ledger_path) == [astuple(ARM)]
    finally:
        tape.dispose()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("branch", ["insert", "noop", "conflict"])
def test_close_failure_preserves_prior_mutation_but_never_exposes_stored_metadata(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    branch: str,
) -> None:
    if branch != "insert":
        put(ledger_path, ARM)
    tape = Tape(monkeypatch, close_fault=when)
    try:
        result = writer.compare_and_set_arm(
            ledger_path,
            journal_id=JOURNAL,
            desired=ARM,
            expected_prior=ARM if branch == "noop" else None,
        )
        assert tape.injected == ["close"] and tape.closes == 1
        assert result.cleanup == CleanupOutcome.UNCERTAIN and result.fault == writer.Fault.CLOSE_FAILURE
        assert result.stored is None
        assert result.mutation == (
            MutationOutcome.COMMITTED if branch == "insert" else MutationOutcome.ROLLED_BACK
        )
        assert result.changed is (branch == "insert")
        assert result.refusal == (writer.Refusal.ARM_CONFLICT if branch == "conflict" else None)
        tape.reconcile()
    finally:
        tape.dispose()


@pytest.mark.parametrize("rollback_when", ["before", "after"])
@pytest.mark.parametrize("close_when", [None, "before", "after"])
@pytest.mark.parametrize("branch", ["commit", "noop", "conflict"])
def test_rollback_and_close_compound_fault_axes_and_selected_fault_precedence(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_when: str,
    close_when: str | None,
    branch: str,
) -> None:
    if branch != "commit":
        put(ledger_path, ARM)
    tape = Tape(
        monkeypatch,
        fail_at=11 if branch == "commit" else None,
        rollback_fault=rollback_when,
        close_fault=close_when,
    )
    try:
        result = writer.compare_and_set_arm(
            ledger_path,
            journal_id=JOURNAL,
            desired=ARM,
            expected_prior=ARM if branch == "noop" else None,
        )
        assert tape.injected.count("ROLLBACK") == 1 and tape.attempts.count("ROLLBACK") == 1
        assert tape.closes == 1 and result.stored is None
        assert result.fault == (writer.Fault.CLOSE_FAILURE if close_when else writer.Fault.ROLLBACK_FAILURE)
        assert result.cleanup == (CleanupOutcome.UNCERTAIN if close_when else CleanupOutcome.CLOSED)
        if branch == "commit" or close_when:
            assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        else:
            assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
        assert result.refusal == (writer.Refusal.ARM_CONFLICT if branch == "conflict" else None)
        tape.reconcile()
    finally:
        tape.dispose()
