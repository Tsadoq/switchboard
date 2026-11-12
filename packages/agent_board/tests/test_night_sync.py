import os
import shutil
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.assignment import NightPolicy, decode
from agent_board.night.ledger import CapturedAssignment, Ledger, LedgerError, Pending
from agent_board.night.sync import (
    Ack,
    BoardRefused,
    BoardState,
    BoardSync,
    BoardUnavailable,
    CaptureReply,
    SyncError,
)
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
        self.held_wakes: list[dict[str, Any]] = []
        self.captures: dict[str, dict[str, Any]] = {}
        self.capture_calls = 0
        self.reply_changes: dict[str, Any] = {}
        self.run_deadline = 5_000.0

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

    def capture(self, run_id: str, payload: Mapping[str, Any]) -> CaptureReply:
        self.calls.append("capture")
        self.capture_calls += 1
        receipt_id = payload["receipt_id"]
        if receipt_id in self.captures:
            snapshot = self.captures[receipt_id]
            return CaptureReply(self._reply(Ack(snapshot["journal_id"], snapshot["counter"])), snapshot)
        if (
            payload["journal_id"] != self.journal_id
            or payload["counter"] != self.counter
            or run_id not in self.active
        ):
            raise BoardRefused("stale_counter")
        ack = self._apply(receipt_id) if not self.lose_next_reply else None
        if ack is None:
            self.lose_next_reply = False
            self._apply(receipt_id)
        snapshot = {
            "journal_id": self.journal_id,
            "counter": self.counter,
            "capture_id": receipt_id,
            "run_id": run_id,
            "principal": "pi@dev",
            "deadline": self.run_deadline,
            "wakes": list(self.held_wakes),
            **self.reply_changes,
        }
        self.captures[receipt_id] = snapshot
        if ack is None:
            raise BoardUnavailable("reply lost")
        return CaptureReply(ack, snapshot)

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
    assert rig.sync.frozen is None, "an outage is not a mismatch"
    rig.board.unreadable = False
    rig.board.journal_id = str(uuid4())
    assert rig.sync.requested_stops() == frozenset()
    assert rig.sync.frozen == "journal_identity_mismatch", "a foreign journal must freeze new starts"


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


POLICY = NightPolicy("sb-runner", "a" * 40, "claude", None, "default", 600)
WAKE = {
    "id": 9,
    "reason": "mention",
    "source_ref": "post:1",
    "summary": "please review",
    "summary_truncated": False,
    "thread_id": 31,
    "topic_id": 31,
}


def _bound(rig: Rig) -> Any:
    assert rig.sync.reconcile() is None
    run_id = rig.reserve()
    rig.journal.claim_start(run_id, FENCE)
    rig.bind(run_id)
    return next(record for record in rig.journal.active() if record.run_id == run_id)


def test_a_capture_is_stored_with_its_assignment_in_the_same_acknowledgement(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    before = rig.ledger.identity().acked_counter

    stored = rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)

    assert stored.assignment is not None
    assignment = decode(stored.assignment)
    assert assignment.run_id == record.run_id and assignment.principal == "pi@dev"
    assert assignment.repository_key == "sb-runner" and assignment.base_commit == "a" * 40
    assert assignment.deadline_utc == 1_850.0, "the server deadline must never extend the local cutoff"
    assert rig.ledger.identity().acked_counter == before + 1 == rig.board.counter
    assert rig.ledger.unacknowledged() == ()


def test_an_acknowledged_capture_is_never_captured_again_or_rebuilt(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    first = rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    rig.board.held_wakes = [{**WAKE, "id": 10}]

    again = rig.sync.capture(
        record, NightPolicy("other", "b" * 40, "claude", None, "auto", 60), cutoff_utc=9e9
    )

    assert again == first
    assert rig.board.capture_calls == 1


def test_a_lost_capture_reply_is_resent_unchanged_and_built_from_the_first_choices(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.lose_next_reply = True

    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    pending = rig.ledger.unacknowledged()
    assert [each.action for each in pending] == ["capture"]
    rig.board.held_wakes = []

    stored = rig.sync.capture(
        record, NightPolicy("other", "b" * 40, "claude", None, "auto", 60), cutoff_utc=9e9
    )

    assert stored.receipt_id == pending[0].receipt_id
    assert stored.assignment is not None
    assignment = decode(stored.assignment)
    assert (assignment.repository_key, assignment.turn_seconds) == ("sb-runner", 600), (
        "retry re-resolved policy"
    )
    assert '"id": 9' in assignment.prompt, "the replayed snapshot was not the one the server stored"


def test_reconciliation_completes_a_capture_whose_reply_was_lost(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.lose_next_reply = True
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)

    assert rig.sync.reconcile() is None

    stored = rig.ledger.captured(record.run_id)
    assert isinstance(stored, CapturedAssignment) and stored.assignment is not None


def test_an_empty_capture_is_stored_as_no_assignment(rig: Rig) -> None:
    record = _bound(rig)

    stored = rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)

    assert stored.assignment is None
    assert rig.ledger.identity().acked_counter == rig.board.counter


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"principal": "claude@dev"}, id="other-principal"),
        pytest.param({"run_id": str(uuid4())}, id="other-run"),
        pytest.param({"capture_id": str(uuid4())}, id="other-receipt"),
        pytest.param({"deadline": float("nan")}, id="nan-deadline"),
        pytest.param({"deadline": True}, id="boolean-deadline"),
        pytest.param({"wakes": [{**WAKE, "command": "rm"}]}, id="wake-outside-the-contract"),
    ],
)
def test_a_capture_reply_that_disagrees_is_never_stored(rig: Rig, change: dict[str, Any]) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.reply_changes = change
    before = rig.ledger.identity().acked_counter

    with pytest.raises(SyncError):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)

    assert not isinstance(rig.ledger.captured(record.run_id), CapturedAssignment)
    assert rig.ledger.identity().acked_counter == before, "an unusable reply moved the ledger counter"


def test_a_capture_needs_a_board_acknowledged_binding_and_an_unfrozen_sync(rig: Rig) -> None:
    assert rig.sync.reconcile() is None
    run_id = rig.reserve()
    rig.journal.claim_start(run_id, FENCE)
    record = rig.journal.active()[0]

    with pytest.raises(SyncError, match="not_bound"):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    rig.sync.frozen = "counter_mismatch"
    with pytest.raises(SyncError, match="frozen"):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    assert rig.board.capture_calls == 0
