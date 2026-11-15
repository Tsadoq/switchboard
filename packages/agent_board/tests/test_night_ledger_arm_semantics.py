import math
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import ledger_arm as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.ledger import Arm, Ledger

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


def change(path: Path, sql: str, parameters: Any = ()) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(sql, parameters)


def refused(result: writer.ArmWriteResult, reason: writer.Refusal) -> None:
    assert result.refusal == reason and result.fault is None and result.stored is None
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
    assert result.cleanup == CleanupOutcome.CLOSED


@pytest.mark.parametrize("prior", [None, ARM, replace(ARM, deadline_utc=2000.000000000001)])
@pytest.mark.parametrize("present", [False, True])
def test_full_prior_tuple_and_absence_conflict_table(
    ledger_path: Path,
    prior: Arm | None,
    present: bool,
) -> None:
    if present:
        put(ledger_path, ARM)
    before = read(ledger_path)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=prior)
    if (not present and prior is None) or (present and prior == ARM):
        assert result.refusal is None and result.fault is None
        assert result.changed is (not present) and result.stored == writer.StoredArm(JOURNAL, ARM)
    else:
        refused(result, writer.Refusal.ARM_CONFLICT)
        assert read(ledger_path) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("guard_boot_id", JOURNAL),
        ("guard_invocation_id", "b" * 32),
        ("deadline_boottime", 1000.0000000000001),
        ("deadline_utc", 2000.000000000001),
    ],
)
def test_each_prior_field_participates_in_comparison_without_tolerance(
    ledger_path: Path,
    field: str,
    value: Any,
) -> None:
    put(ledger_path, ARM)
    result = writer.compare_and_set_arm(
        ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=replace(ARM, **{field: value})
    )
    refused(result, writer.Refusal.ARM_CONFLICT)
    assert read(ledger_path) == [astuple(ARM)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "F0000000-0000-4000-8000-000000000000"),
        ("run_id", "0" * 36),
        ("guard_boot_id", "bad"),
        ("guard_boot_id", b"0" * 36),
        ("guard_invocation_id", "a" * 31),
        ("guard_invocation_id", "a" * 33),
        ("guard_invocation_id", "g" * 32),
        ("guard_invocation_id", 123),
        ("deadline_boottime", float("nan")),
        ("deadline_boottime", float("-inf")),
        ("deadline_boottime", "1000"),
        ("deadline_boottime", False),
        ("deadline_boottime", 1000),
    ],
)
@pytest.mark.parametrize("target", ["desired", "prior"])
def test_both_arm_inputs_revalidated_before_missing_file_access(
    ledger_path: Path,
    field: str,
    value: Any,
    target: str,
) -> None:
    missing = ledger_path.with_name("not-created.sqlite")
    invalid = replace(ARM, **{field: value})
    result = writer.compare_and_set_arm(
        missing,
        journal_id=JOURNAL,
        desired=invalid if target == "desired" else ARM,
        expected_prior=invalid if target == "prior" else None,
    )
    assert result.refusal == writer.Refusal.INVALID_INPUT and result.fault is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and not missing.exists()


@pytest.mark.parametrize("value", [None, True, 123, b"a" * 36, "bad", "F0000000-0000-4000-8000-000000000000"])
def test_journal_id_input_is_exact_canonical_string(ledger_path: Path, value: Any) -> None:
    result = writer.compare_and_set_arm(ledger_path, journal_id=value, desired=ARM, expected_prior=None)
    assert result.refusal == writer.Refusal.INVALID_INPUT and result.fault is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED


@pytest.mark.parametrize("target", ["desired", "prior"])
def test_arm_subclass_and_bypassed_frozen_constructor_are_not_trusted(ledger_path: Path, target: str) -> None:
    class Child(Arm):
        pass

    invalids: list[Any] = [Child(*astuple(ARM)), object.__new__(Arm), astuple(ARM), {"run_id": ARM.run_id}]
    for invalid in invalids:
        result = writer.compare_and_set_arm(
            ledger_path,
            journal_id=JOURNAL,
            desired=invalid if target == "desired" else ARM,
            expected_prior=invalid if target == "prior" else None,
        )
        assert (
            result.refusal == writer.Refusal.INVALID_INPUT
            and result.mutation == MutationOutcome.NOT_ATTEMPTED
        )
    modified = replace(ARM)
    object.__setattr__(modified, "deadline_utc", True)
    result = writer.compare_and_set_arm(
        ledger_path, journal_id=JOURNAL, desired=modified, expected_prior=None
    )
    assert result.refusal == writer.Refusal.INVALID_INPUT


@pytest.mark.parametrize("journal", [None, ARM.run_id])
def test_valid_wrong_or_null_identity_precedes_malformed_arm_and_never_reads_it(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: str | None,
) -> None:
    put(ledger_path, replace(ARM, guard_invocation_id="INVALID"))
    change(ledger_path, "UPDATE identity SET journal_id=?", (journal,))
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    refused(result, writer.Refusal.LEDGER_IDENTITY_MISMATCH)
    assert writer.ARM_QUERY not in tape.attempts and len(tape.attempts) == 9
    tape.reconcile()


@pytest.mark.parametrize(
    "sql,parameters",
    [
        ("DELETE FROM identity", ()),
        ("UPDATE identity SET singleton=2", ()),
        ("UPDATE identity SET journal_id=?", ("bad",)),
        ("UPDATE identity SET journal_id=?", (b"a" * 36,)),
        ("UPDATE identity SET journal_id=?", ("x" * 10000,)),
        ("UPDATE identity SET acked_counter=-1", ()),
        ("UPDATE identity SET acked_counter='bad'", ()),
        ("UPDATE identity SET acked_counter=1.5", ()),
        ("UPDATE identity SET acked_counter=1e30", ()),
        ("INSERT INTO identity VALUES(2,?,0)", (JOURNAL,)),
    ],
)
def test_malformed_identity_precedes_comparison_and_arm_read(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
    parameters: Any,
) -> None:
    change(ledger_path, sql, parameters)
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(ledger_path, journal_id=ARM.run_id, desired=ARM, expected_prior=None)
    refused(result, writer.Refusal.ROW_REFUSED)
    assert writer.ARM_QUERY not in tape.attempts and len(tape.attempts) == 9
    tape.reconcile()


@pytest.mark.parametrize(
    "field,value",
    [
        ("guard_boot_id", "invalid"),
        ("guard_boot_id", "F0000000-0000-4000-8000-000000000000"),
        ("guard_boot_id", b"a" * 36),
        ("guard_boot_id", "a" * 10000),
        ("guard_invocation_id", "A" * 32),
        ("guard_invocation_id", "a" * 33),
        ("guard_invocation_id", "a" * 10000),
        ("guard_invocation_id", b"\xff" * 32),
        ("deadline_boottime", -1.0),
        ("deadline_utc", float("inf")),
        ("deadline_utc", float("-inf")),
        ("deadline_utc", "not-a-number"),
    ],
)
def test_malformed_selected_arm_is_row_refusal_before_prior_conflict(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    put(ledger_path, ARM)
    change(ledger_path, f"UPDATE arms SET {field}=?", (value,))
    before = read(ledger_path)
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    refused(result, writer.Refusal.ROW_REFUSED)
    assert len(tape.attempts) == 10 and not any(sql.startswith(("INSERT", "UPDATE")) for sql in tape.attempts)
    assert read(ledger_path) == before
    tape.reconcile()


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA user_version=0",
        "PRAGMA user_version=2",
        "PRAGMA user_version=4",
        "CREATE TABLE unexpected(value TEXT)",
        "CREATE TABLE unexpectedly_long_" + "x" * 130 + "(value TEXT)",
        "CREATE TRIGGER unexpected AFTER INSERT ON arms BEGIN SELECT 1; END",
        "CREATE INDEX unexpected ON arms(guard_boot_id)",
        "DROP TABLE arms",
        "DROP INDEX one_ready_per_run",
    ],
)
def test_schema_refusal_precedes_identity_or_target_work_and_never_repairs(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
) -> None:
    if sql == "DROP INDEX one_ready_per_run":
        with closing(CONNECT(ledger_path)) as conn:
            index = conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL LIMIT 1"
            ).fetchone()[0]
        sql = f"DROP INDEX {index}"
    change(ledger_path, sql)
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    refused(result, writer.Refusal.SCHEMA_REFUSED)
    assert writer.IDENTITY_QUERY not in tape.attempts and writer.ARM_QUERY not in tape.attempts
    assert len(tape.attempts) <= 8
    tape.reconcile()


def test_signed_zero_and_integer_valued_sqlite_real_affinity_are_not_coerced_inputs(
    ledger_path: Path,
) -> None:
    zero = replace(ARM, deadline_boottime=-0.0, deadline_utc=-0.0)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=zero, expected_prior=None)
    assert result.stored is not None
    assert math.copysign(1.0, result.stored.arm.deadline_utc) == 1.0
    change(ledger_path, "UPDATE arms SET deadline_boottime=1000,deadline_utc=2000")
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=ARM)
    assert result.refusal is None and result.fault is None and result.changed is False
    assert result.mutation == MutationOutcome.ROLLED_BACK


def test_lost_reply_insert_is_not_an_idempotent_absence_retry_and_aba_is_not_detected(
    ledger_path: Path,
) -> None:
    writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    second = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    refused(second, writer.Refusal.ARM_CONFLICT)
    other = replace(ARM, guard_invocation_id="b" * 32)
    legacy = Ledger(ledger_path)
    legacy.arm(other)
    legacy.arm(ARM)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=other, expected_prior=ARM)
    assert result.mutation == MutationOutcome.COMMITTED and result.changed is True
    assert result.stored == writer.StoredArm(JOURNAL, other)
