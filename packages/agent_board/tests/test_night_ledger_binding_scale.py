import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from agent_board.night import ledger_binding as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night_inspection import unit_cgroup

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import CONNECT, JOURNAL, Tape
    from packages.agent_board.tests.test_night_ledger_binding import CLAIM, ledger_path
else:
    from test_night_ledger_arm import CONNECT, JOURNAL, Tape
    from test_night_ledger_binding import CLAIM, ledger_path

__all__ = ["ledger_path"]


def other_rows(conn: sqlite3.Connection) -> dict[str, list[Any]]:
    return {
        table: conn.execute(f"SELECT * FROM {table}").fetchall()
        for table in ("identity", "receipts", "captures", "capture_aborts", "arms", "sqlite_sequence")
    }


def seed(conn: sqlite3.Connection, history: int) -> None:
    def rows() -> Iterator[tuple[str, int, str, str, str, int]]:
        for index in range(history):
            run = str(UUID(int=index + 1))
            yield (
                run,
                CLAIM.uid,
                CLAIM.boot_id,
                CLAIM.invocation_id,
                unit_cgroup(f"switchboard-night-{run}.service"),
                index % 2,
            )

    conn.execute("BEGIN IMMEDIATE")
    conn.executemany("INSERT INTO bindings VALUES(?,?,?,?,?,?)", rows())
    conn.executemany(
        "INSERT INTO receipts(receipt_id,action,run_id,payload) VALUES(?,?,?,?)",
        [
            ("capture-r", "capture", "capture-run", "{}"),
            ("abort-r", "capture_abort", "capture-run", "{}"),
            ("evidence-r", "evidence", "evidence-run", "{}"),
        ],
    )
    conn.execute("INSERT INTO captures VALUES('capture-run','capture-r','pi@dev',100.0,'{}',NULL,NULL)")
    conn.execute("INSERT INTO capture_aborts VALUES('capture-r','abort-r',NULL,NULL)")
    conn.execute(
        "INSERT INTO arms VALUES(?,?,?,?,?)",
        ("other-arm", CLAIM.boot_id, CLAIM.invocation_id, 1000.0, 2000.0),
    )
    conn.execute("COMMIT")


@pytest.mark.parametrize("history", [10, 10000, 100000])
def test_actual_api_every_branch_ack_state_native_trace_and_untouched_history(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: int,
) -> None:
    with closing(CONNECT(ledger_path, isolation_level=None)) as conn:
        seed(conn, history)
        original = other_rows(conn)
        unrelated = conn.execute("SELECT * FROM bindings ORDER BY run_id").fetchall()
        plan = conn.execute("EXPLAIN QUERY PLAN " + writer.BINDING_QUERY, (CLAIM.run_id,)).fetchall()
        assert (
            len(plan) == 1
            and "SEARCH bindings USING INDEX sqlite_autoindex_bindings_1 (run_id=?)" in plan[0][3]
        )
        for ack in (0, 1):
            if ack:
                conn.execute("UPDATE bindings SET server_acked=1 WHERE run_id=?", (CLAIM.run_id,))
            cases = [
                ("match", CLAIM, JOURNAL, 10),
                ("conflict", replace(CLAIM, uid=1001), JOURNAL, 10),
                ("identity", CLAIM, CLAIM.run_id, 9),
            ]
            if not ack:
                cases.insert(0, ("insert", CLAIM, JOURNAL, 12))
            for branch, claim, journal_id, count in cases:
                tape = Tape(monkeypatch)
                try:
                    result = writer.publish_binding(ledger_path, journal_id=journal_id, claim=claim)
                    assert len(tape.native) == len(tape.attempts) == count
                    assert result.fault is None and result.cleanup == CleanupOutcome.CLOSED
                    assert result.refusal == (
                        writer.Refusal.BINDING_CONFLICT
                        if branch == "conflict"
                        else writer.Refusal.LEDGER_IDENTITY_MISMATCH
                        if branch == "identity"
                        else None
                    )
                    assert result.mutation == (
                        MutationOutcome.COMMITTED if branch == "insert" else MutationOutcome.ROLLED_BACK
                    )
                    assert result.changed is (branch == "insert")
                    assert result.stored == (
                        writer.StoredBinding(JOURNAL, CLAIM, bool(ack))
                        if branch in ("insert", "match")
                        else None
                    )
                    tape.reconcile()
                finally:
                    tape.dispose()
        run = "70000000-0000-4000-8000-000000000000"
        extra = replace(CLAIM, run_id=run, cgroup=unit_cgroup(f"switchboard-night-{run}.service"))
        tape = Tape(monkeypatch, fail_at=11)
        try:
            result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=extra)
            assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
            assert (
                result.fault == writer.Fault.SQLITE_FAILURE
                and result.stored is None
                and result.refusal is None
            )
            assert len(tape.attempts) == 13 and len(tape.native) == 12 and len(tape.unexecuted) == 1
            assert tape.attempts[-2:] == ["COMMIT", "ROLLBACK"]
            tape.reconcile()
        finally:
            tape.dispose()
        run = "80000000-0000-4000-8000-000000000000"
        extra = replace(CLAIM, run_id=run, cgroup=unit_cgroup(f"switchboard-night-{run}.service"))
        conn.execute("BEGIN")
        conn.execute("SELECT * FROM identity").fetchall()
        tape = Tape(monkeypatch)
        try:
            result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=extra)
            assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
            assert (
                result.fault == writer.Fault.SQLITE_FAILURE
                and result.stored is None
                and result.refusal is None
            )
            assert len(tape.attempts) == len(tape.native) == 13
            assert tape.native[-2:] == ["COMMIT", "ROLLBACK"]
            tape.reconcile()
        finally:
            tape.dispose()
            conn.execute("ROLLBACK")
        assert other_rows(conn) == original
        assert (
            conn.execute("SELECT * FROM bindings WHERE run_id!=? ORDER BY run_id", (CLAIM.run_id,)).fetchall()
            == unrelated
        )
        assert conn.execute("SELECT * FROM bindings WHERE run_id=?", (CLAIM.run_id,)).fetchall() == [
            (*astuple(CLAIM), 1)
        ]
