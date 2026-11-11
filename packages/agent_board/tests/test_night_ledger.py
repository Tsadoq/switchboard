import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.ledger import Arm, Ledger, LedgerError, Pending

BOOT = str(uuid4())
JOURNAL = str(uuid4())
GUARD = "a" * 32
CGROUP = "/switchboard-night.slice/switchboard-night-x.service"


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
    ledger.record_binding(run_id, boot_id=BOOT, invocation_id="b" * 32, cgroup=CGROUP)
    ledger.record_binding(run_id, boot_id=BOOT, invocation_id="b" * 32, cgroup=CGROUP)

    with pytest.raises(LedgerError, match="binding_conflict"):
        ledger.record_binding(run_id, boot_id=BOOT, invocation_id="c" * 32, cgroup=CGROUP)


def _bound_and_armed(ledger: Ledger, run_id: str, guard: str = GUARD) -> None:
    ledger.record_binding(run_id, boot_id=BOOT, invocation_id="b" * 32, cgroup=CGROUP)
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
    ledger.record_binding(run_id, boot_id=BOOT, invocation_id="b" * 32, cgroup=CGROUP)
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
