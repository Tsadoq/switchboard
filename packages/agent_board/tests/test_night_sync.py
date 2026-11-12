import base64
import json
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.assignment import NightPolicy, decode
from agent_board.night.context import ContextReader, ContextRefused, parse_page
from agent_board.night.ledger import CaptureAborted, CapturedAssignment, Ledger, LedgerError, Pending
from agent_board.night.sync import (
    AbortReply,
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
        self.deny_capture = False
        self.capture_outage = False
        self.tombstones: set[str] = set()
        self.aborts: dict[str, dict[str, Any]] = {}
        self.abort_changes: dict[str, Any] = {}
        self.lose_next_abort_reply = False

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
        if receipt_id in self.tombstones:
            raise BoardRefused("capture_aborted")
        if self.capture_outage:
            raise BoardUnavailable("down before the capture arrived")
        if self.deny_capture and receipt_id not in self.captures:
            raise BoardRefused("not_running")
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

    def capture_abort(self, run_id: str, payload: Mapping[str, Any]) -> AbortReply:
        self.calls.append("capture_abort")
        abort_id = payload["receipt_id"]
        if abort_id in self.aborts:
            proof = self.aborts[abort_id]
        else:
            parent, parent_counter = payload["capture_receipt_id"], payload["capture_counter"]
            if payload["counter"] != parent_counter or payload["journal_id"] != self.journal_id:
                raise BoardRefused("stale_counter")
            if parent in self.captures:
                if self.counter != parent_counter + 1:
                    raise BoardRefused("counter_diverged")
                outcome, capture = "applied", self.captures[parent]
            else:
                if self.counter != parent_counter:
                    raise BoardRefused("counter_diverged")
                self.tombstones.add(parent)
                self._apply(abort_id)
                outcome, capture = "aborted", None
            proof = {
                "journal_id": self.journal_id,
                "run_id": run_id,
                "abort_id": abort_id,
                "capture_id": parent,
                "capture_counter": parent_counter,
                "counter": parent_counter + 1,
                "outcome": outcome,
                "capture": capture,
                **self.abort_changes,
            }
            self.aborts[abort_id] = proof
        if self.lose_next_abort_reply:
            self.lose_next_abort_reply = False
            raise BoardUnavailable("abort reply lost")
        return AbortReply(Ack(proof["journal_id"], proof["counter"]), proof)

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
        pytest.param({"deadline": -1}, id="negative-deadline"),
        pytest.param({"deadline": 10**400}, id="overflowing-deadline"),
        pytest.param({"counter": 999}, id="snapshot-counter-jump"),
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
    assert rig.sync.frozen is not None and rig.sync.frozen.startswith("capture_refused:"), rig.sync.frozen


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


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"deadline": -1}, id="negative-deadline"),
        pytest.param({"deadline": 10**400}, id="overflowing-deadline"),
        pytest.param({"deadline": 0}, id="zero-deadline"),
    ],
)
def test_an_empty_capture_still_needs_a_valid_deadline(rig: Rig, change: dict[str, Any]) -> None:
    record = _bound(rig)
    rig.board.reply_changes = change

    with pytest.raises(SyncError):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)

    assert not isinstance(rig.ledger.captured(record.run_id), CapturedAssignment)


def test_an_acknowledgement_that_jumps_the_counter_is_refused_and_freezes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    original = rig.board.capture

    def jump(run_id: str, payload: Any) -> CaptureReply:
        reply = original(run_id, payload)
        return CaptureReply(Ack(reply.ack.journal_id, 999), {**reply.snapshot, "counter": 999})

    monkeypatch.setattr(rig.board, "capture", jump)
    before = rig.ledger.identity().acked_counter

    with pytest.raises(SyncError, match="capture_counter_mismatch"):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)

    assert rig.ledger.identity().acked_counter == before
    assert rig.sync.frozen == "capture_refused:capture_counter_mismatch"
    with pytest.raises(SyncError, match="frozen"):
        rig.bind(record.run_id)


def test_reconciliation_keeps_refusing_an_unusable_replay(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.reply_changes = {"principal": "claude@dev"}
    with pytest.raises(SyncError):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)

    assert rig.sync.reconcile() == "capture_refused:capture_reply_mismatch"
    assert [each.action for each in rig.ledger.unacknowledged()] == ["capture"], "the receipt was dropped"


def test_a_refused_capture_freezes_but_an_outage_does_not(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    original = rig.board.capture

    def outage(run_id: str, payload: Any) -> CaptureReply:
        raise BoardUnavailable("down")

    monkeypatch.setattr(rig.board, "capture", outage)
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    assert rig.sync.frozen is None, "an outage before any reply is not a mismatch"

    def counted_then_refused(run_id: str, payload: Any) -> CaptureReply:
        original(run_id, payload)
        raise BoardRefused("invalid_capture")

    monkeypatch.setattr(rig.board, "capture", counted_then_refused)
    with pytest.raises(BoardRefused):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    assert rig.sync.frozen == "capture_denied:invalid_capture"
    assert [each.action for each in rig.ledger.unacknowledged()] == ["capture"]


def _stopping(rig: Rig, record: Any) -> Any:
    rig.journal.request_stop(record.run_id, FENCE)
    return next(each for each in rig.journal.active() if each.run_id == record.run_id)


def _evidence_counters(rig: Rig) -> list[int]:
    return [payload["counter"] for payload in rig.board.evidence_payloads]


def test_an_outage_blocks_no_read_but_every_new_receipt_waits_for_the_pending_capture(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.capture_outage = True
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    assert rig.sync.frozen is None, "an outage is not a mismatch"
    rig.board.capture_outage = False
    counter_before = rig.board.counter

    rig.bind(record.run_id)

    assert rig.board.calls[-2:] == ["capture", "binding"], (
        "the new binding went out before the pending capture"
    )
    assert rig.board.counter == counter_before + 2
    assert rig.ledger.identity().acked_counter == rig.board.counter
    assert rig.ledger.unacknowledged() == ()


def test_a_capture_denied_by_a_stop_race_is_aborted_and_then_evidence_settles(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.deny_capture = True
    with pytest.raises(BoardRefused):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    assert rig.sync.frozen == "capture_denied:not_running"
    parent_counter = rig.ledger.identity().acked_counter

    assert rig.sync.settle(_stopping(rig, record), "preempted")

    assert rig.board.calls[-2:] == ["capture_abort", "evidence"]
    assert isinstance(rig.ledger.captured(record.run_id), CaptureAborted)
    assert _evidence_counters(rig) == [parent_counter + 1], "evidence did not follow the abort's counter"
    assert rig.sync.frozen is None, "the denial freeze outlived a clean resolution"
    assert rig.ledger.identity().acked_counter == parent_counter + 2


def test_a_capture_the_board_already_applied_resolves_as_applied_and_is_never_launchable(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.lose_next_reply = True
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    parent_counter = rig.ledger.unacknowledged()[0].payload["counter"]

    assert rig.sync.settle(_stopping(rig, record), "preempted")

    stored = rig.ledger.captured(record.run_id)
    assert (
        isinstance(stored, CapturedAssignment)
        and stored.assignment is not None
        and stored.aborted_after_apply
    )
    assert rig.board.counter == parent_counter + 2, "the applied resolution counted a second mutation"
    assert _evidence_counters(rig) == [parent_counter + 1]
    with pytest.raises(SyncError, match="capture_aborted"):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)


def test_once_an_abort_is_linked_the_parent_is_never_sent_again(rig: Rig, tmp_path: Path) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.capture_outage = True
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    rig.board.lose_next_abort_reply = True
    stopping = _stopping(rig, record)
    assert not rig.sync.settle(stopping, "preempted")
    rig.board.capture_outage = False
    captures_before = rig.board.capture_calls

    restarted = BoardSync(
        "dev", Ledger(rig.state_dir / "ledger.sqlite3"), rig.board, rig.inspector, rig.journal
    )
    assert restarted.reconcile() is None
    with pytest.raises(SyncError, match="capture_aborted"):
        restarted.capture(record, POLICY, cutoff_utc=1_850.0)

    assert rig.board.capture_calls == captures_before, "a parent with a linked abort was resent"
    assert isinstance(restarted._ledger.captured(record.run_id), CaptureAborted)
    assert restarted.settle(stopping, "preempted")


@pytest.mark.parametrize("boundary", ["before-capture-send", "before-abort-send", "abort-reply-lost"])
def test_a_crash_at_each_boundary_resumes_into_one_resolution(rig: Rig, boundary: str) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.capture_outage = True
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    rig.board.capture_outage = False
    stopping = _stopping(rig, record)
    if boundary == "before-abort-send":
        parent = rig.ledger.unacknowledged()[0]
        abort_id = str(uuid4())
        rig.ledger.persist_abort(
            parent.receipt_id,
            Pending(
                abort_id,
                "capture_abort",
                record.run_id,
                {
                    **parent.payload,
                    "receipt_id": abort_id,
                    "capture_receipt_id": parent.receipt_id,
                    "capture_counter": parent.payload["counter"],
                },
            ),
        )
    if boundary == "abort-reply-lost":
        rig.board.lose_next_abort_reply = True
        assert not rig.sync.settle(stopping, "preempted")

    restarted = BoardSync(
        "dev", Ledger(rig.state_dir / "ledger.sqlite3"), rig.board, rig.inspector, rig.journal
    )
    assert restarted.reconcile() is None
    assert restarted.settle(stopping, "preempted")

    resolved = restarted._ledger.captured(record.run_id)
    if boundary == "before-capture-send":
        assert isinstance(resolved, CapturedAssignment) and not rig.board.tombstones, (
            "with no abort linked yet, reconcile resends the unchanged capture"
        )
    else:
        assert isinstance(resolved, CaptureAborted) and len(rig.board.tombstones) == 1
    assert len(rig.board.evidence_payloads) == 1
    assert restarted._ledger.unacknowledged() == ()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"abort_id": str(uuid4())}, id="other-abort"),
        pytest.param({"capture_id": str(uuid4())}, id="other-parent"),
        pytest.param({"run_id": str(uuid4())}, id="other-run"),
        pytest.param({"journal_id": str(uuid4())}, id="other-journal"),
        pytest.param({"counter": 999}, id="counter-jump"),
        pytest.param({"capture_counter": 0}, id="other-parent-counter"),
        pytest.param({"outcome": "dropped"}, id="unknown-outcome"),
        pytest.param({"capture": {"wakes": []}}, id="aborted-with-a-capture"),
    ],
)
def test_a_malformed_abort_proof_stays_pending_and_frozen(rig: Rig, change: dict[str, Any]) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.deny_capture = True
    with pytest.raises(BoardRefused):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    rig.board.abort_changes = change

    assert not rig.sync.settle(_stopping(rig, record), "preempted")

    assert rig.sync.frozen is not None and rig.sync.frozen.startswith("capture_refused:"), rig.sync.frozen
    assert [each.action for each in rig.ledger.unacknowledged()] == ["capture_abort"]
    assert rig.board.evidence_payloads == [], "evidence went out before the capture was resolved"


def test_an_applied_capture_with_unrelated_divergence_stays_denied(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.lose_next_reply = True
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    rig.board.counter += 1

    assert not rig.sync.settle(_stopping(rig, record), "preempted")

    assert rig.sync.frozen == "capture_denied:counter_diverged"
    assert [each.action for each in rig.ledger.unacknowledged()] == ["capture_abort"]
    assert rig.board.evidence_payloads == []


def test_a_capture_denial_survives_a_stop_poll_and_still_recovers(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.deny_capture = True
    with pytest.raises(BoardRefused):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    stopping = _stopping(rig, record)
    rig.board.stopping.add(record.run_id)

    assert record.run_id in rig.sync.requested_stops()
    assert rig.sync.frozen == "capture_denied:not_running", "a reconcile erased the typed denial"

    assert rig.sync.settle(stopping, "preempted"), rig.sync.frozen


def test_a_restart_before_the_abort_link_keeps_the_denial_recoverable(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.deny_capture = True
    with pytest.raises(BoardRefused):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    stopping = _stopping(rig, record)
    restarted = BoardSync(
        "dev", Ledger(rig.state_dir / "ledger.sqlite3"), rig.board, rig.inspector, rig.journal
    )

    assert restarted.reconcile() == "capture_denied:not_running"
    assert restarted.settle(stopping, "preempted"), restarted.frozen
    assert restarted.frozen is None


@pytest.mark.parametrize("field", ["capture_counter", "counter"])
def test_abort_proof_counters_must_be_exact_integers(rig: Rig, field: str) -> None:
    record = _bound(rig)
    rig.board.deny_capture = True
    with pytest.raises(BoardRefused):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    counter = rig.ledger.identity().acked_counter
    rig.board.abort_changes = {field: float(counter if field == "capture_counter" else counter + 1)}

    assert not rig.sync.settle(_stopping(rig, record), "preempted")

    assert rig.board.evidence_payloads == []
    assert [each.action for each in rig.ledger.unacknowledged()] == ["capture_abort"]


def test_an_applied_snapshot_counter_must_be_an_exact_integer(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.held_wakes = [WAKE]
    rig.board.lose_next_reply = True
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    parent = next(iter(rig.board.captures))
    rig.board.captures[parent] = {**rig.board.captures[parent], "counter": float(rig.board.counter)}

    assert not rig.sync.settle(_stopping(rig, record), "preempted")

    assert rig.sync.frozen is not None and rig.sync.frozen.startswith("capture_refused:"), rig.sync.frozen
    assert rig.board.evidence_payloads == []


def test_no_abort_is_composed_for_an_unlatched_or_still_running_run(rig: Rig) -> None:
    record = _bound(rig)
    rig.board.capture_outage = True
    with pytest.raises(BoardUnavailable):
        rig.sync.capture(record, POLICY, cutoff_utc=1_850.0)
    rig.board.capture_outage = False
    rig.inspector.not_stopped = InspectionError("still_running")

    assert not rig.sync.settle(record, "completed")
    rig.inspector.not_stopped = None
    assert not rig.sync.settle(record, "completed"), (
        "an abort was composed for a run whose stop is not latched"
    )

    captured = rig.ledger.captured(record.run_id)
    assert isinstance(captured, Pending) and captured.action == "capture"
    assert "capture_abort" not in rig.board.calls


from board.night import Run
from board.night_context import ContextDenied, read_context

THREAD = 31


class ServerBoard:
    """The real server read_context over a minimal board database, with optional response tampering."""

    def __init__(self, capture_response: Callable[[], Mapping[str, Any]]) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for statement in (
            "CREATE TABLE night_wake_captures (receipt_id TEXT PRIMARY KEY, run_id TEXT)",
            "CREATE TABLE night_verifier_receipts (receipt_id TEXT PRIMARY KEY, response TEXT)",
            "CREATE TABLE threads (id INTEGER PRIMARY KEY, title TEXT NOT NULL)",
            (
                "CREATE TABLE posts (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, author TEXT NOT NULL, "
                "author_kind TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL)"
            ),
            (
                "CREATE TABLE night_context_reads (run_id TEXT PRIMARY KEY, reads INTEGER NOT NULL DEFAULT 0, "
                "bytes INTEGER NOT NULL DEFAULT 0)"
            ),
        ):
            self.conn.execute(statement)
        self.conn.execute(
            "INSERT INTO threads (id, title) VALUES (?, ?)", (THREAD, "Bounded opt-in nightly pilot")
        )
        self._capture_response = capture_response
        self.calls = 0
        self.tamper: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def post(self, body: str, post_id: int | None = None) -> int:
        cursor = self.conn.execute(
            "INSERT INTO posts (id, thread_id, author, author_kind, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (post_id, THREAD, "pi@dev", "agent", body, 1_000.0),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def thread_read(self, run_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        response = self._capture_response()
        self.conn.execute(
            "INSERT OR IGNORE INTO night_wake_captures (receipt_id, run_id) VALUES (?, ?)",
            (payload["capture_id"], run_id),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO night_verifier_receipts (receipt_id, response) VALUES (?, ?)",
            (payload["capture_id"], json.dumps(response)),
        )
        run = Run(
            run_id, "pi@dev", "dev", "2026-11-12", payload["fence"], 0.0, 0.0, 9e9, 0.0, None, "running"
        )
        try:
            data = read_context(
                self.conn,
                run,
                payload["capture_id"],
                payload["thread_id"],
                payload["post_id"],
                payload["body_offset"],
            )
        except ContextDenied as denied:
            raise BoardRefused(str(denied)) from None
        return self.tamper(dict(data)) if self.tamper is not None else data


class Context:
    def __init__(self, tmp_path: Path) -> None:
        self.rig = Rig(tmp_path)
        self.record = _bound(self.rig)
        self.rig.board.held_wakes = [{**WAKE, "thread_id": THREAD, "topic_id": THREAD}]
        self.rig.sync.capture(self.record, POLICY, cutoff_utc=1_850.0)
        receipt = next(iter(self.rig.board.captures))
        self.server = ServerBoard(lambda: self.rig.board.captures[receipt])
        self.reader = ContextReader(self.server, self.rig.ledger)


@pytest.fixture
def context(tmp_path: Path) -> Context:
    return Context(tmp_path)


def test_a_short_post_is_read_whole_in_one_call(context: Context) -> None:
    post_id = context.server.post("please review PR 39")

    post = context.reader.post(context.record, THREAD, None)

    assert post is not None and post.post_id == post_id
    assert post.complete and post.text == "please review PR 39"
    assert context.server.calls == 1


def test_a_long_post_is_reassembled_contiguously_across_split_utf8_before_decoding(context: Context) -> None:
    body = "é" * 1535 + "x" + "日本語" * 2000
    assert len(body.encode()) > 3 * 3072
    context.server.post(body)

    post = context.reader.post(context.record, THREAD, None)

    assert post is not None and post.complete and post.text == body
    assert context.server.calls == -(-len(body.encode()) // 3072)


def test_a_body_over_the_read_ceiling_stays_explicitly_incomplete(context: Context) -> None:
    context.server.post("a" * 300_000)

    post = context.reader.post(context.record, THREAD, None)

    assert post is not None
    assert not post.complete and post.text is None
    assert len(post.body) == 262_144 and post.body_total_bytes == 300_000


def test_older_posts_are_reached_only_through_previous_post_id(context: Context) -> None:
    older = context.server.post("first")
    newer = context.server.post("second")

    newest = context.reader.post(context.record, THREAD, None)
    assert newest is not None and newest.post_id == newer and newest.previous_post_id == older

    previous = context.reader.post(context.record, THREAD, newest.previous_post_id)
    assert previous is not None and previous.text == "first" and previous.previous_post_id is None


def test_an_empty_thread_reads_as_no_post(context: Context) -> None:
    assert context.reader.post(context.record, THREAD, None) is None


@pytest.mark.parametrize(
    ("thread_id", "post_id", "offset", "reason"),
    [
        pytest.param(54, None, 0, "thread_not_captured", id="thread-outside-the-capture"),
        pytest.param(True, None, 0, "thread_not_captured", id="boolean-thread"),
        pytest.param(THREAD, None, 5, "missing_post_cursor", id="offset-without-post"),
        pytest.param(THREAD, 1, -1, "invalid_body_offset", id="negative-offset"),
        pytest.param(THREAD, 1, 262_144, "invalid_body_offset", id="offset-at-the-ceiling"),
        pytest.param(THREAD, 0, 0, "invalid_post", id="zero-post"),
        pytest.param(THREAD, 2**63, 0, "invalid_post", id="post-past-signed-64"),
    ],
)
def test_a_request_outside_the_contract_never_reaches_the_board(
    context: Context, thread_id: Any, post_id: Any, offset: Any, reason: str
) -> None:
    with pytest.raises(ContextRefused, match=reason):
        context.reader.page(context.record, thread_id, post_id, offset)

    assert context.server.calls == 0


def test_a_stopping_run_reads_nothing(context: Context) -> None:
    stopping = _stopping(context.rig, context.record)

    with pytest.raises(ContextRefused, match="run_not_running"):
        context.reader.page(stopping, THREAD, None, 0)

    assert context.server.calls == 0


def test_a_server_refusal_such_as_an_exhausted_quota_stays_an_error(context: Context) -> None:
    context.server.post("x")
    context.server.conn.execute(
        "INSERT INTO night_context_reads (run_id, reads, bytes) VALUES (?, 128, 0)", (context.record.run_id,)
    )

    with pytest.raises(BoardRefused, match="context_read_limit"):
        context.reader.post(context.record, THREAD, None)


def _set_post(**changes: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def tamper(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "post": {**data["post"], **changes}}

    return tamper


def _set(**changes: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def tamper(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, **changes}

    return tamper


def _short_chunk(data: dict[str, Any]) -> dict[str, Any]:
    body = base64.b64decode(data["post"]["body_base64"])[:-1]
    end = data["post"]["body_offset"] + len(body)
    return {
        **data,
        "post": {**data["post"], "body_base64": base64.b64encode(body).decode(), "next_body_offset": end},
    }


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(_set(extra=1), id="extra-field"),
        pytest.param(_set(run_id="00000000-0000-4000-8000-000000000000"), id="other-run"),
        pytest.param(_set(thread_id=float(THREAD)), id="float-thread"),
        pytest.param(_set(title="t" * 129), id="title-over-bound"),
        pytest.param(_set(title_truncated=1), id="truncation-not-boolean"),
        pytest.param(_set(previous_post_id=10**9), id="previous-not-older"),
        pytest.param(_set_post(body_offset=1.0), id="float-offset"),
        pytest.param(_set_post(body_base64="AA AA"), id="base64-with-space"),
        pytest.param(_set_post(body_base64="AAA"), id="base64-unpadded"),
        pytest.param(_set_post(next_body_offset=9000), id="cursor-skips-bytes"),
        pytest.param(_set_post(body_end_reached=False), id="end-flag-lies"),
        pytest.param(_set_post(body_limit_reached=True), id="limit-flag-lies"),
        pytest.param(_set_post(body_total_bytes=-1), id="negative-total"),
        pytest.param(_set_post(created_at=True), id="boolean-time"),
        pytest.param(_set_post(author="a" * 129), id="author-over-bound"),
        pytest.param(_short_chunk, id="short-chunk"),
    ],
)
def test_a_response_outside_the_contract_is_refused(
    context: Context, tamper: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    context.server.post("é" * 5000)
    context.server.tamper = tamper

    with pytest.raises(ContextRefused):
        context.reader.post(context.record, THREAD, None)


def test_a_chunk_for_another_post_or_offset_breaks_the_read(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = context.server.post("a" * 5000)
    context.server.post("b" * 10)
    real = context.server.thread_read
    calls = {"n": 0}

    def wrong_offset(run_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls["n"] += 1
        if calls["n"] == 2:
            return real(run_id, {**payload, "body_offset": 0})
        return real(run_id, payload)

    monkeypatch.setattr(context.server, "thread_read", wrong_offset)

    with pytest.raises(ContextRefused):
        context.reader.post(context.record, THREAD, first)


def test_an_empty_thread_response_cannot_name_a_previous_post() -> None:
    data = {
        "run_id": "r",
        "capture_id": "c",
        "thread_id": THREAD,
        "title": "t",
        "title_truncated": False,
        "post": None,
        "previous_post_id": 5,
    }

    with pytest.raises(ContextRefused):
        parse_page(data, run_id="r", capture_id="c", thread_id=THREAD, post_id=None, body_offset=0)


def test_no_read_touches_the_ledger_counter_or_wakes(context: Context) -> None:
    context.server.post("x")
    before = context.rig.ledger.identity()

    context.reader.post(context.record, THREAD, None)

    assert context.rig.ledger.identity() == before
    assert context.rig.ledger.unacknowledged() == ()
    assert FENCE == context.record.fence
