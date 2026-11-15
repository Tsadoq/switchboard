from dataclasses import astuple
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import ledger_binding as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import JOURNAL, Tape
    from packages.agent_board.tests.test_night_ledger_binding import CLAIM, ledger_path, put, read
else:
    from test_night_ledger_arm import JOURNAL, Tape
    from test_night_ledger_binding import CLAIM, ledger_path, put, read

__all__ = ["ledger_path"]


@pytest.mark.parametrize("stage", range(12))
@pytest.mark.parametrize("when", ["before", "after"])
def test_every_insert_statement_fault_native_trace_and_actual_durable_state(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: int,
    when: str,
) -> None:
    tape = Tape(monkeypatch, fail_at=stage, when=when)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert len(tape.injected) == 1 and result.fault == writer.Fault.SQLITE_FAILURE
        assert result.refusal is None and result.stored is None and result.cleanup == CleanupOutcome.CLOSED
        assert tape.closes == 1 and tape.attempts.count("ROLLBACK") <= 1
        assert len(tape.attempts) <= (13 if stage == 11 else 12)
        if stage <= 3:
            assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
            if stage == 3 and when == "after":
                assert tape.attempts[-1] == "ROLLBACK"
        elif stage == 11:
            assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        else:
            assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
        tape.reconcile()
        assert read(ledger_path) == ([(*astuple(CLAIM), 0)] if stage == 11 and when == "after" else [])
    finally:
        tape.dispose()


@pytest.mark.parametrize("ack", [0, 1])
@pytest.mark.parametrize("stage", range(10))
@pytest.mark.parametrize("when", ["before", "after"])
def test_every_match_statement_fault_with_each_ack_never_commits_or_exposes_success(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ack: int,
    stage: int,
    when: str,
) -> None:
    put(ledger_path, ack=ack)
    tape = Tape(monkeypatch, fail_at=stage, when=when)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert len(tape.injected) == 1 and result.stored is None and result.refusal is None
        assert result.fault == (writer.Fault.ROLLBACK_FAILURE if stage == 9 else writer.Fault.SQLITE_FAILURE)
        assert result.mutation == (
            MutationOutcome.NOT_ATTEMPTED if stage <= 3 else MutationOutcome.ROLLED_BACK
        )
        assert result.changed is False and result.cleanup == CleanupOutcome.CLOSED
        assert "COMMIT" not in tape.attempts and tape.attempts.count("ROLLBACK") <= 1
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in tape.attempts)
        assert len(tape.attempts) <= 10 and tape.closes == 1
        tape.reconcile()
        assert read(ledger_path) == [(*astuple(CLAIM), ack)]
    finally:
        tape.dispose()


@pytest.mark.parametrize("branch", ["insert", "match0", "match1", "conflict0", "conflict1"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_known_commit_or_rollback_survives_close_fault_but_stored_is_never_usable(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    when: str,
) -> None:
    from dataclasses import replace

    ack = int(branch.endswith("1"))
    if branch != "insert":
        put(ledger_path, ack=ack)
    claim = replace(CLAIM, uid=1001) if branch.startswith("conflict") else CLAIM
    tape = Tape(monkeypatch, close_fault=when)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=claim)
        assert tape.injected == ["close"] and tape.closes == 1
        assert result.fault == writer.Fault.CLOSE_FAILURE and result.cleanup == CleanupOutcome.UNCERTAIN
        assert result.mutation == (
            MutationOutcome.COMMITTED if branch == "insert" else MutationOutcome.ROLLED_BACK
        )
        assert result.changed is (branch == "insert") and result.stored is None
        assert result.refusal == (writer.Refusal.BINDING_CONFLICT if branch.startswith("conflict") else None)
        assert read(ledger_path) == [(*astuple(CLAIM), ack)]
        tape.reconcile()
    finally:
        tape.dispose()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("close_when", [None, "before", "after"])
def test_ambiguous_commit_with_rollback_and_close_fault_selection_remains_unknown(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    close_when: str | None,
) -> None:
    tape = Tape(monkeypatch, fail_at=11, rollback_fault=when, close_fault=close_when)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert tape.injected.count("COMMIT") == tape.injected.count("ROLLBACK") == 1
        assert tape.attempts.count("ROLLBACK") == tape.closes == 1
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        assert result.stored is None and result.refusal is None
        assert result.fault == (writer.Fault.CLOSE_FAILURE if close_when else writer.Fault.ROLLBACK_FAILURE)
        assert result.cleanup == (CleanupOutcome.UNCERTAIN if close_when else CleanupOutcome.CLOSED)
        tape.reconcile()
    finally:
        tape.dispose()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("close_when", ["before", "after"])
def test_after_native_commit_exception_then_close_fault_keeps_durable_state_distinct_from_knowledge(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    close_when: str,
) -> None:
    tape = Tape(monkeypatch, fail_at=11, when=when, close_fault=close_when)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        assert result.fault == writer.Fault.CLOSE_FAILURE and result.cleanup == CleanupOutcome.UNCERTAIN
        assert result.stored is None and result.refusal is None
        assert read(ledger_path) == ([(*astuple(CLAIM), 0)] if when == "after" else [])
        tape.reconcile()
    finally:
        tape.dispose()
