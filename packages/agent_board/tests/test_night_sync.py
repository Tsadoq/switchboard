import os
import shutil
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.ledger import Ledger, LedgerError, Pending
from agent_board.night.sync import Ack, BoardRefused, BoardState, BoardSync, BoardUnavailable, SyncError
from agent_board.night_inspection import ROOT_EXEC, Binding, EmptyObservation, Execution, InspectionError
from agent_board.night_state import ClockReading, Grant, LocalJournal

BOOT = str(uuid4())
UID = 2001
FENCE = "f" * 43


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


class Board:
    def __init__(self) -> None:
        self.journal_id: str | None = None
        self.counter = 0
        self.active: set[str] = set()
        self.acks: dict[str, Ack] = {}
        self.calls: list[str] = []
        self.lose_next_reply = False
        self.keep_held = False
        self.drift_after_evidence = False
        self.evidence_payloads: list[dict[str, Any]] = []
        self.stopping: set[str] = set()
        self.unreadable = False

    def state(self) -> BoardState:
        if self.unreadable:
            raise BoardUnavailable("board down")
        return BoardState(
            "dev", self.journal_id, self.counter, frozenset(self.active), frozenset(self.stopping)
        )

    def bootstrap(self, payload: Mapping[str, Any]) -> Ack:
        self.calls.append("bootstrap")
        if payload["receipt_id"] in self.acks:
            return self._reply(self.acks[payload["receipt_id"]])
        if self.journal_id is not None or self.active:
            raise BoardRefused("identity_exists")
        self.journal_id = payload["journal_id"]
        return self._apply(payload["receipt_id"])

    def binding(self, run_id: str, payload: Mapping[str, Any]) -> Ack:
        self.calls.append("binding")
        if payload["receipt_id"] in self.acks:
            return self._reply(self.acks[payload["receipt_id"]])
        if payload["journal_id"] != self.journal_id or payload["counter"] != self.counter:
            raise BoardRefused("stale_counter")
        self.active.add(run_id)
        return self._apply(payload["receipt_id"])

    def evidence(self, run_id: str, payload: Mapping[str, Any]) -> Ack:
        self.calls.append("evidence")
        self.evidence_payloads.append(dict(payload))
        if payload["receipt_id"] in self.acks:
            return self._reply(self.acks[payload["receipt_id"]])
        if (
            payload["journal_id"] != self.journal_id
            or payload["counter"] != self.counter
            or run_id not in self.active
        ):
            raise BoardRefused("stale_counter")
        if not self.keep_held:
            self.active.discard(run_id)
        ack = self._apply(payload["receipt_id"])
        if self.drift_after_evidence:
            self.counter += 1
        return ack

    def _apply(self, receipt_id: str) -> Ack:
        assert self.journal_id is not None
        self.counter += 1
        self.acks[receipt_id] = Ack(self.journal_id, self.counter)
        return self._reply(self.acks[receipt_id])

    def _reply(self, ack: Ack) -> Ack:
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise BoardUnavailable("reply lost")
        return ack


class Inspector:
    def __init__(self) -> None:
        self.failure: InspectionError | None = None
        self.not_stopped: InspectionError | None = None
        self.barrier: threading.Barrier | None = None
        self.executions: list[str] = []

    def capture(self, *, run_id: str, uid: int, boot_id: str, execution: Execution) -> Binding:
        self.executions.append(execution)
        if self.failure is not None:
            raise self.failure
        return Binding(run_id, uid, boot_id, "b" * 32, execution)

    def stopped(self, binding: Binding) -> EmptyObservation:
        if self.barrier is not None:
            try:
                self.barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
        if self.not_stopped is not None:
            raise self.not_stopped
        return EmptyObservation(binding, cgroup_present=False, unit_loaded=False)


class Rig:
    def __init__(self, tmp_path: Path, board: Board | None = None) -> None:
        self.state_dir = tmp_path / "state"
        self.clock = ClockReading(BOOT, 100.0, 1_000.0)
        self.journal = LocalJournal(
            self.state_dir / "journal.sqlite3",
            box="dev",
            principals={"pi@dev": UID},
            clock=lambda: self.clock,
        )
        self.board = board or Board()
        self.inspector = Inspector()
        self.ledger = Ledger(self.state_dir / "ledger.sqlite3")
        self.sync = BoardSync("dev", self.ledger, self.board, self.inspector, self.journal)

    def reserve(self) -> str:
        run_id = str(uuid4())
        self.journal.reserve(Grant(run_id, FENCE, "pi@dev", UID, BOOT, 900.0, 1_900.0))
        return run_id

    def bind(self, run_id: str) -> None:
        self.sync.bind(run_id=run_id, fence=FENCE, uid=UID, boot_id=BOOT)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_fresh_box_bootstraps_once_and_unfreezes(rig: Rig) -> None:
    assert rig.sync.reconcile() is None
    assert rig.sync.reconcile() is None

    assert rig.board.calls == ["bootstrap"], rig.board.calls
    assert rig.ledger.identity().journal_id == rig.board.journal_id is not None


def test_new_starts_are_frozen_until_reconciled(rig: Rig) -> None:
    with pytest.raises(SyncError, match="not_reconciled"):
        rig.bind(rig.reserve())


def test_a_bootstrap_whose_reply_was_lost_is_resent_identically(rig: Rig) -> None:
    rig.board.lose_next_reply = True

    first = rig.sync.reconcile()
    second = rig.sync.reconcile()

    assert first is not None and first.startswith("reconcile_failed"), first
    assert second is None, second
    assert rig.board.counter == 1, "the bootstrap was applied twice"


def test_a_wiped_ledger_does_not_pass_as_a_fresh_box(rig: Rig, tmp_path: Path) -> None:
    rig.sync.reconcile()
    (rig.state_dir / "ledger.sqlite3").unlink()

    restarted = Rig(tmp_path, rig.board)

    assert restarted.sync.reconcile() == "journal_identity_mismatch"
    assert rig.board.calls == ["bootstrap"], "a second bootstrap was attempted"


def test_a_rolled_back_ledger_is_caught_by_the_counter(rig: Rig, tmp_path: Path) -> None:
    rig.sync.reconcile()
    snapshot = tmp_path / "snapshot.sqlite3"
    shutil.copy(rig.state_dir / "ledger.sqlite3", snapshot)
    rig.bind(rig.reserve())
    shutil.copy(snapshot, rig.state_dir / "ledger.sqlite3")

    restored = BoardSync(
        "dev", Ledger(rig.state_dir / "ledger.sqlite3"), rig.board, rig.inspector, rig.journal
    )

    assert restored.reconcile() == "counter_mismatch"


def test_a_board_run_missing_from_the_local_journal_freezes(rig: Rig) -> None:
    rig.sync.reconcile()
    rig.board.active.add(str(uuid4()))

    assert rig.sync.reconcile() == "missing_local_run"


class VaultBoard(Board):
    def state(self) -> BoardState:
        return BoardState("vault", None, 0, frozenset())


def test_a_board_for_another_box_freezes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, VaultBoard())

    assert rig.sync.reconcile() == "wrong_box"


def test_binding_records_the_unit_with_the_board_and_a_lost_reply_is_replayed(rig: Rig) -> None:
    rig.sync.reconcile()
    run_id = rig.reserve()
    rig.board.lose_next_reply = True

    with pytest.raises(BoardUnavailable):
        rig.bind(run_id)
    assert rig.ledger.unacknowledged(), "the binding message was not kept for replay"
    assert rig.sync.reconcile() is None

    assert run_id in rig.board.active and rig.board.counter == 2, (rig.board.active, rig.board.counter)
    assert rig.ledger.unacknowledged() == ()


def test_a_unit_that_cannot_be_inspected_is_not_bound(rig: Rig) -> None:
    rig.sync.reconcile()
    rig.inspector.failure = InspectionError("binding_conflict")

    with pytest.raises(SyncError, match="binding_conflict"):
        rig.bind(rig.reserve())
    assert rig.board.calls == ["bootstrap"], rig.board.calls


def _stopping_run(rig: Rig) -> Any:
    rig.sync.reconcile()
    run_id = rig.reserve()
    rig.journal.claim_start(run_id, FENCE)
    rig.bind(run_id)
    return rig.journal.request_stop(run_id, FENCE)


def test_a_proven_empty_run_is_reported_once_and_released_when_the_board_agrees(rig: Rig) -> None:
    record = _stopping_run(rig)

    settled = rig.sync.settle(record, "completed")

    assert settled, "a proven, board-settled run was not released"
    assert rig.journal.active() == ()
    [payload] = rig.board.evidence_payloads
    assert (
        payload["outcome"] == "empty"
        and payload["runtime_seconds"] is None
        and payload["reason"] == "completed"
    )
    assert payload["counter"] == 2 and payload["cgroup"].endswith(
        f"switchboard-night-{record.run_id}.service"
    )


def test_a_run_the_board_still_holds_keeps_its_slot(rig: Rig) -> None:
    record = _stopping_run(rig)
    rig.board.keep_held = True

    assert not rig.sync.settle(record, "deadline")
    assert [each.run_id for each in rig.journal.active()] == [record.run_id]


def test_no_evidence_is_sent_for_a_run_that_is_not_proven_stopped(rig: Rig) -> None:
    record = _stopping_run(rig)
    rig.inspector.not_stopped = InspectionError("populated_or_unknown")

    assert not rig.sync.settle(record, "completed")
    assert rig.board.evidence_payloads == []


def test_a_lost_evidence_reply_is_resent_identically_and_applied_once(rig: Rig) -> None:
    record = _stopping_run(rig)
    rig.board.lose_next_reply = True

    first = rig.sync.settle(record, "completed")
    second = rig.sync.settle(record, "completed")

    assert not first and second
    assert rig.board.counter == 3, rig.board.counter
    assert {payload["receipt_id"] for payload in rig.board.evidence_payloads} == {
        rig.board.evidence_payloads[0]["receipt_id"]
    }


def test_counter_drift_after_the_report_keeps_the_slot(rig: Rig) -> None:
    record = _stopping_run(rig)
    rig.board.drift_after_evidence = True

    assert not rig.sync.settle(record, "completed")
    assert [each.run_id for each in rig.journal.active()] == [record.run_id]


def test_a_run_never_bound_with_the_board_is_not_released(rig: Rig) -> None:
    rig.sync.reconcile()
    run_id = rig.reserve()
    rig.journal.claim_start(run_id, FENCE)
    record = rig.journal.request_stop(run_id, FENCE)

    assert not rig.sync.settle(record, "completed")
    assert rig.board.evidence_payloads == []


def test_concurrent_stop_and_sweep_settle_a_run_with_one_evidence_receipt(rig: Rig) -> None:
    record = _stopping_run(rig)
    rig.inspector.barrier = threading.Barrier(2)
    results: list[bool] = []

    workers = [
        threading.Thread(target=lambda: results.append(rig.sync.settle(record, "completed")))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    receipts = {payload["receipt_id"] for payload in rig.board.evidence_payloads}
    assert len(receipts) == 1, f"concurrent settles composed {len(receipts)} evidence receipts"
    assert rig.ledger.unacknowledged() == (), "an orphaned evidence receipt was left behind"
    assert rig.journal.active() == () and True in results, results


def test_the_ledger_refuses_a_second_evidence_message_for_one_run(rig: Rig) -> None:
    run_id = str(uuid4())
    rig.ledger.persist(Pending(str(uuid4()), "evidence", run_id, {"receipt_id": "a"}))

    with pytest.raises(LedgerError):
        rig.ledger.persist(Pending(str(uuid4()), "evidence", run_id, {"receipt_id": "b"}))


def test_the_runtime_always_captures_root_exec_units_with_the_native_uid(rig: Rig) -> None:
    rig.sync.reconcile()
    run_id = rig.reserve()
    rig.journal.claim_start(run_id, FENCE)

    rig.bind(run_id)

    assert rig.inspector.executions == [ROOT_EXEC]
    recorded = rig.ledger.binding(run_id)
    assert recorded is not None and recorded.uid == UID, "the native uid was not kept for accounting"


def test_stop_requests_are_the_boards_stopping_runs_for_this_journal(rig: Rig) -> None:
    rig.sync.reconcile()
    run_id = rig.reserve()
    rig.journal.claim_start(run_id, FENCE)
    rig.bind(run_id)
    rig.board.stopping = {run_id, str(uuid4())}

    assert rig.sync.requested_stops() == {run_id}, "a stop for a run the board no longer holds was passed on"


def test_unreadable_or_foreign_board_state_requests_no_stop(rig: Rig) -> None:
    rig.sync.reconcile()
    run_id = rig.reserve()
    rig.journal.claim_start(run_id, FENCE)
    rig.bind(run_id)
    rig.board.stopping = {run_id}

    rig.board.unreadable = True
    assert rig.sync.requested_stops() == frozenset()
    rig.board.unreadable = False
    rig.board.journal_id = str(uuid4())
    assert rig.sync.requested_stops() == frozenset()


def test_a_stopping_run_the_journal_does_not_know_freezes_new_starts(rig: Rig) -> None:
    assert rig.sync.reconcile() is None
    known = rig.reserve()
    rig.journal.claim_start(known, FENCE)
    rig.bind(known)
    unknown = str(uuid4())
    rig.board.active.add(unknown)
    rig.board.stopping = {known, unknown}

    requested = rig.sync.requested_stops()

    assert rig.sync.frozen == "missing_local_run"
    assert requested == {known}, "a known run the board wants stopped must still be stopped while frozen"


def test_a_counter_the_ledger_did_not_produce_freezes_new_starts(rig: Rig) -> None:
    assert rig.sync.reconcile() is None
    rig.board.counter += 1

    rig.sync.requested_stops()

    assert rig.sync.frozen == "counter_mismatch"
