import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.assignment import NightPolicy
from agent_board.night.ledger import (
    LEDGER_VERSION,
    Arm,
    CaptureAborted,
    CaptureContext,
    CapturedAssignment,
    Ledger,
    LedgerError,
    Pending,
)

BOOT = str(uuid4())
JOURNAL = str(uuid4())
GUARD = "a" * 32
CGROUP = "/switchboard.slice/switchboard-night.slice/switchboard-night-x.service"


@pytest.fixture(autouse=True)
def protected_fixture_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def metadata(path: Path) -> os.stat_result:
        result = path.lstat()
        if path in tmp_path.parents:
            values = list(result)
            values[0] = 0o40755
            values[4] = 0
            return os.stat_result(values)
        return result

    monkeypatch.setattr(night_state, "_ancestor_stat", metadata, raising=False)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "state" / "ledger.sqlite3")


def _binding_receipt(run_id: str) -> Pending:
    return Pending(str(uuid4()), "binding", run_id, {"fence": "f" * 43, "counter": 0, "cgroup": CGROUP})


def test_a_message_is_persisted_whole_and_resent_identically_until_acknowledged(tmp_path: Path) -> None:
    path = tmp_path / "state" / "ledger.sqlite3"
    receipt = _binding_receipt(str(uuid4()))
    Ledger(path).persist(receipt)

    after_restart = Ledger(path)

    assert after_restart.unacknowledged() == (receipt,), after_restart.unacknowledged()
    after_restart.acknowledge(receipt.receipt_id, journal_id=JOURNAL, counter=1)
    assert after_restart.unacknowledged() == ()
    assert after_restart.identity().journal_id == JOURNAL and after_restart.identity().acked_counter == 1


def test_acknowledgements_cannot_regress_the_counter_or_change_the_journal(ledger: Ledger) -> None:
    first, second, third = (_binding_receipt(str(uuid4())) for _ in range(3))
    for each in (first, second, third):
        ledger.persist(each)
    ledger.acknowledge(first.receipt_id, journal_id=JOURNAL, counter=5)

    with pytest.raises(LedgerError, match="counter_regressed"):
        ledger.acknowledge(second.receipt_id, journal_id=JOURNAL, counter=4)
    with pytest.raises(LedgerError, match="journal_identity_mismatch"):
        ledger.acknowledge(third.receipt_id, journal_id=str(uuid4()), counter=6)
    assert ledger.identity().acked_counter == 5


def test_an_unknown_or_repeated_acknowledgement_is_refused(ledger: Ledger) -> None:
    receipt = _binding_receipt(str(uuid4()))
    ledger.persist(receipt)
    ledger.acknowledge(receipt.receipt_id, journal_id=JOURNAL, counter=1)

    with pytest.raises(LedgerError, match="unknown_receipt"):
        ledger.acknowledge(receipt.receipt_id, journal_id=JOURNAL, counter=2)
    with pytest.raises(LedgerError, match="unknown_receipt"):
        ledger.acknowledge(str(uuid4()), journal_id=JOURNAL, counter=2)


def test_a_binding_cannot_be_rebound_to_another_invocation(ledger: Ledger) -> None:
    run_id = str(uuid4())
    ledger.record_binding(run_id, uid=2001, boot_id=BOOT, invocation_id="b" * 32, cgroup=CGROUP)
    ledger.record_binding(run_id, uid=2001, boot_id=BOOT, invocation_id="b" * 32, cgroup=CGROUP)

    with pytest.raises(LedgerError, match="binding_conflict"):
        ledger.record_binding(run_id, uid=2001, boot_id=BOOT, invocation_id="c" * 32, cgroup=CGROUP)


def _bound_and_armed(ledger: Ledger, run_id: str, guard: str = GUARD) -> None:
    ledger.record_binding(run_id, uid=2001, boot_id=BOOT, invocation_id="b" * 32, cgroup=CGROUP)
    receipt = _binding_receipt(run_id)
    ledger.persist(receipt)
    ledger.acknowledge(receipt.receipt_id, journal_id=JOURNAL, counter=1)
    ledger.arm(Arm(run_id, BOOT, guard, 500.0, 2_000.0))


def test_ready_needs_the_board_ack_and_the_current_guards_arm_for_the_exact_deadlines(ledger: Ledger) -> None:
    run_id = str(uuid4())
    _bound_and_armed(ledger, run_id)

    def ready(boot_id: str = BOOT, guard: str = GUARD, deadline_boottime: float = 500.0) -> bool:
        return ledger.ready(
            run_id,
            boot_id=boot_id,
            guard_invocation_id=guard,
            deadline_boottime=deadline_boottime,
            deadline_utc=2_000.0,
        )

    assert ready()
    assert not ready(guard="d" * 32), "an arm from an earlier guard satisfied readiness"
    assert not ready(boot_id=str(uuid4())), "an arm from an earlier boot satisfied readiness"
    assert not ready(deadline_boottime=600.0), "an arm for a different deadline satisfied readiness"


def test_ready_is_false_without_the_boards_binding_ack(ledger: Ledger) -> None:
    run_id = str(uuid4())
    ledger.record_binding(run_id, uid=2001, boot_id=BOOT, invocation_id="b" * 32, cgroup=CGROUP)
    ledger.arm(Arm(run_id, BOOT, GUARD, 500.0, 2_000.0))

    assert not ledger.ready(
        run_id, boot_id=BOOT, guard_invocation_id=GUARD, deadline_boottime=500.0, deadline_utc=2_000.0
    )


def test_an_unknown_ledger_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state" / "ledger.sqlite3"
    Ledger(path)

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 9")

    with pytest.raises(LedgerError, match="unsupported_ledger_version"):
        Ledger(path)


POLICY = NightPolicy("sb-runner", "a" * 40, "claude", None, "default", 600)


def _capture_receipt(run_id: str) -> Pending:
    return Pending(str(uuid4()), "capture", run_id, {"fence": "f" * 43, "counter": 1, "cgroup": CGROUP})


def test_a_capture_is_acknowledged_only_together_with_its_snapshot_and_assignment(ledger: Ledger) -> None:
    run_id = str(uuid4())
    receipt = _capture_receipt(run_id)
    ledger.persist_capture(receipt, CaptureContext("pi@dev", 1_850.0, POLICY))
    assert ledger.captured(run_id) == receipt
    assert ledger.capture_context(receipt.receipt_id) == CaptureContext("pi@dev", 1_850.0, POLICY)

    with pytest.raises(LedgerError, match="capture_acknowledgement_mismatch"):
        ledger.acknowledge(receipt.receipt_id, journal_id=JOURNAL, counter=2)
    assert ledger.unacknowledged() == (receipt,), "a refused acknowledgement left a partial state"
    assert ledger.identity().acked_counter == 0

    ledger.acknowledge(receipt.receipt_id, journal_id=JOURNAL, counter=2, capture=('{"wakes": []}', b"{}"))

    assert ledger.captured(run_id) == CapturedAssignment(run_id, receipt.receipt_id, '{"wakes": []}', b"{}")
    assert ledger.identity().acked_counter == 2


def test_a_non_capture_acknowledgement_cannot_carry_capture_data(ledger: Ledger) -> None:
    receipt = _binding_receipt(str(uuid4()))
    ledger.persist(receipt)

    with pytest.raises(LedgerError, match="capture_acknowledgement_mismatch"):
        ledger.acknowledge(receipt.receipt_id, journal_id=JOURNAL, counter=1, capture=("{}", None))
    assert ledger.unacknowledged() == (receipt,)


def test_a_run_has_at_most_one_capture(ledger: Ledger) -> None:
    run_id = str(uuid4())
    ledger.persist_capture(_capture_receipt(run_id), CaptureContext("pi@dev", 1_850.0, POLICY))

    with pytest.raises(LedgerError, match="capture_exists"):
        ledger.persist_capture(_capture_receipt(run_id), CaptureContext("pi@dev", 1_850.0, POLICY))


def test_a_version_1_ledger_is_migrated_with_every_receipt_kept(tmp_path: Path) -> None:
    path = tmp_path / "state" / "ledger.sqlite3"
    Ledger(path)
    receipt = _binding_receipt(str(uuid4()))
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE receipts")
        conn.execute("DROP TABLE captures")
        conn.execute(
            "CREATE TABLE receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL UNIQUE, "
            "action TEXT NOT NULL CHECK(action IN ('bootstrap', 'binding', 'evidence')), run_id TEXT, "
            "payload TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0 CHECK(acked IN (0, 1)))"
        )
        conn.execute(
            "INSERT INTO receipts (seq, receipt_id, action, run_id, payload) VALUES (7, ?, 'binding', ?, ?)",
            (
                receipt.receipt_id,
                receipt.run_id,
                '{"cgroup": "' + CGROUP + '", "counter": 0, "fence": "' + "f" * 43 + '"}',
            ),
        )
        conn.execute("PRAGMA user_version = 1")

    migrated = Ledger(path)

    assert migrated.unacknowledged() == (receipt,)
    run_id = str(uuid4())
    migrated.persist_capture(_capture_receipt(run_id), CaptureContext("pi@dev", 1_850.0, POLICY))
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LEDGER_VERSION
        assert conn.execute(
            "SELECT seq FROM receipts WHERE receipt_id = ?", (receipt.receipt_id,)
        ).fetchone() == (7,)


def _abort_for(parent: Pending) -> Pending:
    abort_id = str(uuid4())
    return Pending(
        abort_id,
        "capture_abort",
        parent.run_id,
        {
            **parent.payload,
            "receipt_id": abort_id,
            "capture_receipt_id": parent.receipt_id,
            "capture_counter": 1,
        },
    )


def test_an_abort_replaces_its_parent_among_the_receipts_to_send(ledger: Ledger) -> None:
    run_id = str(uuid4())
    parent = _capture_receipt(run_id)
    ledger.persist_capture(parent, CaptureContext("pi@dev", 1_850.0, POLICY))
    abort = _abort_for(parent)

    ledger.persist_abort(parent.receipt_id, abort)

    assert ledger.unacknowledged() == (abort,)
    assert ledger.captured(run_id) == abort
    assert ledger.abort_parent(abort.receipt_id) == parent
    with pytest.raises(LedgerError, match="abort_exists"):
        ledger.persist_abort(parent.receipt_id, _abort_for(parent))


def test_an_abort_needs_an_unacknowledged_capture_of_the_same_run(ledger: Ledger) -> None:
    run_id = str(uuid4())
    parent = _capture_receipt(run_id)
    ledger.persist_capture(parent, CaptureContext("pi@dev", 1_850.0, POLICY))
    foreign = _abort_for(parent)

    with pytest.raises(LedgerError, match="abort_parent_not_pending"):
        ledger.persist_abort(
            parent.receipt_id, Pending(foreign.receipt_id, "capture_abort", str(uuid4()), foreign.payload)
        )
    ledger.acknowledge(parent.receipt_id, journal_id=JOURNAL, counter=2, capture=("{}", None))
    with pytest.raises(LedgerError, match="abort_parent_not_pending"):
        ledger.persist_abort(parent.receipt_id, _abort_for(parent))


def test_an_aborted_resolution_never_acknowledges_the_parent(ledger: Ledger) -> None:
    run_id = str(uuid4())
    parent = _capture_receipt(run_id)
    ledger.persist_capture(parent, CaptureContext("pi@dev", 1_850.0, POLICY))
    first = _binding_receipt(str(uuid4()))
    ledger.persist(first)
    ledger.acknowledge(first.receipt_id, journal_id=JOURNAL, counter=1)
    abort = _abort_for(parent)
    ledger.persist_abort(parent.receipt_id, abort)

    ledger.resolve_abort(
        abort.receipt_id, journal_id=JOURNAL, counter=2, proof='{"outcome": "aborted"}', applied=None
    )

    assert ledger.captured(run_id) == CaptureAborted(run_id, parent.receipt_id, '{"outcome": "aborted"}')
    assert ledger.unacknowledged() == ()
    assert ledger.identity().acked_counter == 2
    with pytest.raises(LedgerError, match="unknown_abort"):
        ledger.resolve_abort(abort.receipt_id, journal_id=JOURNAL, counter=3, proof="{}", applied=None)


def test_a_version_2_ledger_gains_capture_aborts_with_its_receipts_kept(tmp_path: Path) -> None:
    path = tmp_path / "state" / "ledger.sqlite3"
    Ledger(path)
    receipt = _binding_receipt(str(uuid4()))
    Ledger(path).persist(receipt)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE capture_aborts")
        conn.execute("ALTER TABLE receipts RENAME TO receipts_v3")
        conn.execute(
            "CREATE TABLE receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL UNIQUE, "
            "action TEXT NOT NULL CHECK(action IN ('bootstrap', 'binding', 'evidence', 'capture')), run_id TEXT, "
            "payload TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0 CHECK(acked IN (0, 1)))"
        )
        conn.execute("INSERT INTO receipts SELECT * FROM receipts_v3")
        conn.execute("DROP TABLE receipts_v3")
        conn.execute("PRAGMA user_version = 2")

    migrated = Ledger(path)

    assert migrated.unacknowledged() == (receipt,)
    run_id = str(uuid4())
    parent = _capture_receipt(run_id)
    migrated.persist_capture(parent, CaptureContext("pi@dev", 1_850.0, POLICY))
    migrated.persist_abort(parent.receipt_id, _abort_for(parent))
