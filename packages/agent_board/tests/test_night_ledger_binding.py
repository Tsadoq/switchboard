from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import ledger_binding as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.ledger import Ledger
from agent_board.night_inspection import unit_cgroup

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import CONNECT, JOURNAL, Tape, ledger_path
else:
    from test_night_ledger_arm import CONNECT, JOURNAL, Tape, ledger_path

__all__ = ["ledger_path"]
RUN = "20000000-0000-4000-8000-000000000000"
CLAIM = writer.BindingClaim(
    RUN,
    1000,
    "30000000-0000-4000-8000-000000000000",
    "a" * 32,
    unit_cgroup(f"switchboard-night-{RUN}.service"),
)


def put(path: Path, claim: writer.BindingClaim = CLAIM, ack: int = 0) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?)", (*astuple(claim), ack))


def read(path: Path) -> list[tuple[Any, ...]]:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        return conn.execute("SELECT * FROM bindings ORDER BY run_id").fetchall()


@pytest.mark.parametrize(
    "branch,count",
    [("insert", 12), ("match0", 10), ("match1", 10), ("conflict0", 10), ("conflict1", 10), ("identity", 9)],
)
def test_transition_table_actual_native_traces_and_historical_ack_snapshot(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    count: int,
) -> None:
    ack = int(branch.endswith("1"))
    if branch.startswith(("match", "conflict")):
        put(ledger_path, ack=ack)
    claim = replace(CLAIM, uid=1001) if branch.startswith("conflict") else CLAIM
    before = read(ledger_path)
    tape = Tape(monkeypatch)
    result = writer.publish_binding(
        ledger_path, journal_id=JOURNAL if branch != "identity" else RUN, claim=claim
    )
    assert len(tape.attempts) == len(tape.native) == count and tape.closes == 1
    assert result.cleanup == CleanupOutcome.CLOSED and result.fault is None
    tape.reconcile()
    if branch.startswith("conflict") or branch == "identity":
        assert result.refusal == (
            writer.Refusal.LEDGER_IDENTITY_MISMATCH
            if branch == "identity"
            else writer.Refusal.BINDING_CONFLICT
        )
        assert (
            result.mutation == MutationOutcome.ROLLED_BACK
            and result.changed is False
            and result.stored is None
        )
        assert read(ledger_path) == before
    else:
        assert result.refusal is None and result.stored == writer.StoredBinding(JOURNAL, claim, bool(ack))
        assert result.mutation == (
            MutationOutcome.COMMITTED if branch == "insert" else MutationOutcome.ROLLED_BACK
        )
        assert result.changed is (branch == "insert")
        assert result.stored is not None and not hasattr(result.stored, "server_acked")
        assert result.stored.observed_server_acked is bool(ack)
        assert read(ledger_path) == [(*astuple(claim), ack)]
    if branch != "insert":
        assert "COMMIT" not in tape.attempts and tape.attempts[-1] == "ROLLBACK"
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in tape.attempts)
    if branch == "identity":
        assert writer.BINDING_QUERY not in tape.attempts


@pytest.mark.parametrize(
    "field,value", [("uid", 0), ("uid", -1), ("invocation_id", "0" * 32), ("cgroup", "/non-canonical")]
)
@pytest.mark.parametrize("ack", [0, 1])
def test_values_persistable_by_legacy_api_are_malformed_not_binding_conflict(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
    ack: int,
) -> None:
    invalid = replace(CLAIM, **{field: value})
    legacy = Ledger(ledger_path)
    legacy.record_binding(
        invalid.run_id,
        uid=invalid.uid,
        boot_id=invalid.boot_id,
        invocation_id=invalid.invocation_id,
        cgroup=invalid.cgroup,
    )
    with closing(CONNECT(ledger_path, isolation_level=None)) as conn:
        conn.execute("UPDATE bindings SET server_acked=?", (ack,))
    before = read(ledger_path)
    tape = Tape(monkeypatch)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
    assert result.refusal == writer.Refusal.ROW_REFUSED and result.fault is None
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.stored is None
    assert len(tape.attempts) == len(tape.native) == 10
    assert read(ledger_path) == before
    tape.reconcile()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("close_when", [None, "before", "after"])
def test_ack1_match_rollback_failure_never_promotes_preserved_bit_into_success(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    close_when: str | None,
) -> None:
    put(ledger_path, ack=1)
    tape = Tape(monkeypatch, rollback_fault=when, close_fault=close_when)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert result.stored is None and result.refusal is None
        assert result.fault == (writer.Fault.CLOSE_FAILURE if close_when else writer.Fault.ROLLBACK_FAILURE)
        assert result.cleanup == (CleanupOutcome.UNCERTAIN if close_when else CleanupOutcome.CLOSED)
        assert result.mutation == (MutationOutcome.UNKNOWN if close_when else MutationOutcome.ROLLED_BACK)
        assert result.changed is (None if close_when else False)
        assert "COMMIT" not in tape.attempts and tape.attempts.count("ROLLBACK") == 1
        assert len(tape.attempts) == 10 and tape.closes == 1
        assert read(ledger_path) == [(*astuple(CLAIM), 1)]
        tape.reconcile()
    finally:
        tape.dispose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("uid", True),
        ("uid", 0),
        ("uid", 1.0),
        ("uid", 2**63),
        ("invocation_id", "0" * 32),
        ("invocation_id", "A" * 32),
        ("cgroup", "/non-canonical"),
        ("cgroup", "\ud800"),
        ("boot_id", "bad"),
    ],
)
def test_invalid_claims_precede_a_missing_file_and_sqlite(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    missing = ledger_path.with_name("absent.sqlite")
    tape = Tape(monkeypatch)
    result = writer.publish_binding(missing, journal_id=JOURNAL, claim=replace(CLAIM, **{field: value}))
    assert result.refusal == writer.Refusal.INVALID_INPUT and result.fault is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and not missing.exists()
    assert not tape.attempts and not tape.connections


@pytest.mark.parametrize("writer_mode", ["reserved", "journal"])
def test_real_legacy_writer_is_busy_without_journal_or_presence_refusal_with_one(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_mode: str,
) -> None:
    legacy = Ledger(ledger_path)
    with legacy._transaction() as conn:
        if writer_mode == "journal":
            conn.execute("INSERT INTO bindings VALUES(?,?,?,?,?,0)", astuple(CLAIM))
        tape = Tape(monkeypatch)
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert result.refusal == (
            writer.Refusal.BUSY if writer_mode == "reserved" else writer.Refusal.SIDECAR_PRESENT
        )
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.cleanup == CleanupOutcome.CLOSED
        assert result.fault is None and result.stored is None and conn.in_transaction
        if writer_mode == "journal":
            assert not tape.attempts and not tape.connections
        else:
            assert tape.attempts[-1] == "BEGIN IMMEDIATE" and tape.closes == 1


def test_real_reader_blocks_insert_commit_and_rollback_does_not_rewrite_unknown(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(CONNECT(ledger_path, isolation_level=None, timeout=0)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM identity").fetchall()
        tape = Tape(monkeypatch)
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        assert result.refusal is None and result.fault == writer.Fault.SQLITE_FAILURE
        assert result.cleanup == CleanupOutcome.CLOSED and result.stored is None
        assert len(tape.attempts) == len(tape.native) == 13
        assert tape.native[-2:] == ["COMMIT", "ROLLBACK"]
        reader.execute("ROLLBACK")
    assert read(ledger_path) == []
