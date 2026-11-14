from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from agent_board.night import ledger_arm as writer
from agent_board.night.journal_latch import MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path
else:
    from test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path

__all__ = ["ledger_path"]


def non_arm_rows(conn: Any) -> dict[str, list[Any]]:
    tables = ("identity", "receipts", "captures", "capture_aborts", "bindings", "sqlite_sequence")
    return {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}


@pytest.mark.parametrize("history", [10, 10000, 100000])
def test_real_api_all_branches_native_counts_and_search_plans_with_growing_history(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: int,
) -> None:
    with closing(CONNECT(ledger_path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO arms VALUES(?,?,?,?,?)",
            (
                (str(UUID(int=index + 1)), ARM.guard_boot_id, ARM.guard_invocation_id, 1000.0, 2000.0)
                for index in range(history)
            ),
        )
        conn.execute("COMMIT")
        before = non_arm_rows(conn)
        unrelated = conn.execute("SELECT * FROM arms ORDER BY run_id").fetchall()
        plan = conn.execute("EXPLAIN QUERY PLAN " + writer.ARM_QUERY, (ARM.run_id,)).fetchall()
        assert len(plan) == 1
        assert "SEARCH arms USING INDEX sqlite_autoindex_arms_1 (run_id=?)" in plan[0][3]
        updated = replace(ARM, guard_invocation_id="b" * 32)
        cases = (
            (ARM, None, JOURNAL, 12, MutationOutcome.COMMITTED, None),
            (updated, ARM, JOURNAL, 12, MutationOutcome.COMMITTED, None),
            (updated, updated, JOURNAL, 10, MutationOutcome.ROLLED_BACK, None),
            (ARM, None, JOURNAL, 10, MutationOutcome.ROLLED_BACK, writer.Refusal.ARM_CONFLICT),
            (
                ARM,
                updated,
                ARM.run_id,
                9,
                MutationOutcome.ROLLED_BACK,
                writer.Refusal.LEDGER_IDENTITY_MISMATCH,
            ),
        )
        for desired, expected, journal_id, budget, mutation, refusal in cases:
            tape = Tape(monkeypatch)
            try:
                result = writer.compare_and_set_arm(
                    ledger_path, journal_id=journal_id, desired=desired, expected_prior=expected
                )
                assert result.mutation == mutation and result.refusal == refusal and result.fault is None
                assert len(tape.native) == len(tape.attempts) == budget
                assert result.stored == (writer.StoredArm(JOURNAL, desired) if refusal is None else None)
                assert result.changed is (mutation == MutationOutcome.COMMITTED)
                tape.reconcile()
            finally:
                tape.dispose()
        tape = Tape(monkeypatch, fail_at=11)
        try:
            result = writer.compare_and_set_arm(
                ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=updated
            )
            assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
            assert (
                result.fault == writer.Fault.SQLITE_FAILURE
                and result.refusal is None
                and result.stored is None
            )
            assert len(tape.attempts) == 13 and len(tape.native) == 12 and len(tape.unexecuted) == 1
            assert tape.attempts[-2:] == ["COMMIT", "ROLLBACK"]
            tape.reconcile()
        finally:
            tape.dispose()
        assert non_arm_rows(conn) == before
        assert (
            conn.execute("SELECT * FROM arms WHERE run_id!=? ORDER BY run_id", (ARM.run_id,)).fetchall()
            == unrelated
        )
        assert conn.execute("SELECT * FROM arms WHERE run_id=?", (ARM.run_id,)).fetchall() == [
            astuple(updated)
        ]
