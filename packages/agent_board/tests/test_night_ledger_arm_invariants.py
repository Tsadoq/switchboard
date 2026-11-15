import sqlite3
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import ledger_arm as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import (
        ARM,
        CONNECT,
        JOURNAL,
        Tape,
        ledger_path,
        put,
        read,
    )
else:
    from test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path, put, read

__all__ = ["ledger_path"]


@pytest.mark.parametrize("branch", ["insert", "replace"])
@pytest.mark.parametrize(
    "anomaly", ["rowcount_zero", "rowcount_multiple", "missing", "mismatch", "malformed", "multiple"]
)
def test_post_dml_invariants_are_faults_not_late_semantic_refusals_and_roll_back_actual_changes(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    anomaly: str,
) -> None:
    prior = ARM if branch == "replace" else None
    desired = replace(ARM, guard_invocation_id="b" * 32)
    if prior is not None:
        put(ledger_path, prior)
    calls: list[str] = []
    source_native: list[str] = []
    fixture_native: list[str] = []
    fixture_phase = False
    arm_reads = 0

    class Rows:
        def __init__(self, rows: list[sqlite3.Row]) -> None:
            self.rows = rows

        def fetchall(self) -> list[sqlite3.Row]:
            return self.rows + self.rows

    class Rowcount:
        rowcount = 0 if anomaly == "rowcount_zero" else 2

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> Any:
            nonlocal fixture_phase, arm_reads
            calls.append(sql)
            if sql == writer.ARM_QUERY:
                arm_reads += 1
                if arm_reads == 2:
                    fixture_phase = True
                    if anomaly == "missing":
                        super().execute("DELETE FROM arms WHERE run_id=?", (ARM.run_id,))
                    elif anomaly == "mismatch":
                        super().execute("UPDATE arms SET deadline_utc=9999 WHERE run_id=?", (ARM.run_id,))
                    elif anomaly == "malformed":
                        super().execute(
                            "UPDATE arms SET guard_invocation_id='invalid' WHERE run_id=?", (ARM.run_id,)
                        )
                    fixture_phase = False
            cursor = super().execute(sql, parameters)
            if sql.startswith(("INSERT", "UPDATE")) and anomaly.startswith("rowcount"):
                return Rowcount()
            if sql == writer.ARM_QUERY and arm_reads == 2 and anomaly == "multiple":
                return Rows(cursor.fetchall())
            return cursor

    def trace(sql: str) -> None:
        (fixture_native if fixture_phase else source_native).append(sql)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = CONNECT(*args, **kwargs, factory=Connection)
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    result = writer.compare_and_set_arm(
        ledger_path, journal_id=JOURNAL, desired=desired, expected_prior=prior
    )
    assert result.fault == writer.Fault.SNAPSHOT_FAILURE and result.refusal is None
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
    assert result.cleanup == CleanupOutcome.CLOSED and result.stored is None
    assert len(calls) == len(source_native) == (11 if anomaly.startswith("rowcount") else 12)
    assert len(fixture_native) == (1 if anomaly in ("missing", "mismatch", "malformed") else 0)
    assert calls[-1] == "ROLLBACK" and "COMMIT" not in calls
    assert read(ledger_path) == ([] if prior is None else [astuple(prior)])


@pytest.mark.parametrize(
    "code",
    [
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_BUSY_RECOVERY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_LOCKED_SHAREDCACHE,
        None,
        True,
    ],
)
@pytest.mark.parametrize("stage", [0, 3, 4, 11])
def test_typed_primary_extended_codes_and_phase_not_message_define_busy_refusal(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int | None,
    stage: int,
) -> None:
    index = 0
    native: list[str] = []
    attempts: list[str] = []

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            nonlocal index
            attempts.append(sql)
            current, index = index, index + 1
            if current == stage:
                error = sqlite3.OperationalError("database is busy or locked")
                if code is not None:
                    error.sqlite_errorcode = code
                raise error
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = CONNECT(*args, **kwargs, factory=Connection)
        conn.set_trace_callback(native.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    typed_busy = type(code) is int
    assert result.refusal == (writer.Refusal.BUSY if typed_busy and stage < 4 else None)
    assert result.fault == (None if typed_busy and stage < 4 else writer.Fault.SQLITE_FAILURE)
    assert result.mutation == (
        MutationOutcome.NOT_ATTEMPTED
        if stage < 4
        else MutationOutcome.UNKNOWN
        if stage == 11
        else MutationOutcome.ROLLED_BACK
    )
    assert result.changed is (None if stage == 11 else False)
    assert result.cleanup == CleanupOutcome.CLOSED and result.stored is None
    assert len(native) + 1 == len(attempts) and read(ledger_path) == []


@pytest.mark.parametrize("field", ["guard_boot_id", "guard_invocation_id"])
def test_actual_invalid_utf8_text_refuses_without_sqlite_automatic_text_decoding(
    ledger_path: Path,
    field: str,
) -> None:
    put(ledger_path, ARM)
    with closing(CONNECT(ledger_path, isolation_level=None)) as conn:
        conn.execute(f"UPDATE arms SET {field}=CAST(x'fffe' AS TEXT)")
    before = ledger_path.read_bytes()
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert result.refusal == writer.Refusal.ROW_REFUSED and result.fault is None
    assert result.mutation == MutationOutcome.ROLLED_BACK and ledger_path.read_bytes() == before


@pytest.mark.parametrize("field,cap", [("guard_boot_id", 36), ("guard_invocation_id", 32)])
def test_real_projection_caps_large_persisted_text_and_api_refuses_not_truncates(
    ledger_path: Path,
    field: str,
    cap: int,
) -> None:
    put(ledger_path, ARM)
    with closing(CONNECT(ledger_path, isolation_level=None)) as conn:
        conn.execute(f"UPDATE arms SET {field}=?", ("x" * 1000000,))
        conn.row_factory = sqlite3.Row
        projected = conn.execute(writer.ARM_QUERY, (ARM.run_id,)).fetchone()
        assert type(projected[field]) is bytes and len(projected[field]) == cap + 1
        assert projected[field + "_kind"] == "text"
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert result.refusal == writer.Refusal.ROW_REFUSED and result.stored is None


@pytest.mark.parametrize("close_when", ["before", "after"])
def test_after_native_commit_error_and_close_failure_stays_unknown_even_when_durable(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    close_when: str,
) -> None:
    tape = Tape(monkeypatch, fail_at=11, when="after", close_fault=close_when)
    try:
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        assert result.fault == writer.Fault.CLOSE_FAILURE and result.cleanup == CleanupOutcome.UNCERTAIN
        assert result.refusal is None and result.stored is None and read(ledger_path) == [astuple(ARM)]
        assert "ROLLBACK" not in tape.attempts
        tape.reconcile()
    finally:
        tape.dispose()
