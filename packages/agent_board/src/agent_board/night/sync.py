import json
import logging
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from agent_board.night.assignment import AssignmentError, NightPolicy, build_assignment, encode, parse_wakes
from agent_board.night.ledger import CaptureContext, CapturedAssignment, Ledger, LedgerError, Pending
from agent_board.night_inspection import ROOT_EXEC, Binding, EmptyObservation, Execution, InspectionError
from agent_board.night_state import LocalJournal, Record, StateError

logger = logging.getLogger(__name__)


class SyncError(RuntimeError):
    pass


class BoardRefused(SyncError):
    """The board answered with a mismatch (409) or an authorization refusal (401)."""


class BoardUnavailable(SyncError):
    pass


class CaptureRefused(SyncError):
    """The board counted a capture whose reply this box will not use; new starts stay frozen."""


@dataclass(frozen=True)
class BoardState:
    box: str
    journal_id: str | None
    counter: int
    active_run_ids: frozenset[str]
    stop_requested: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Ack:
    journal_id: str
    counter: int


@dataclass(frozen=True)
class CaptureReply:
    ack: Ack
    snapshot: Mapping[str, Any]


class VerifierBoard(Protocol):
    """The verifier's signed /api/night/verifier actions; each raises BoardRefused or BoardUnavailable."""

    def state(self) -> BoardState: ...

    def bootstrap(self, payload: Mapping[str, Any]) -> Ack: ...

    def binding(self, run_id: str, payload: Mapping[str, Any]) -> Ack: ...

    def evidence(self, run_id: str, payload: Mapping[str, Any]) -> Ack: ...

    def capture(self, run_id: str, payload: Mapping[str, Any]) -> CaptureReply: ...


class BindingInspector(Protocol):
    def capture(self, *, run_id: str, uid: int, boot_id: str, execution: Execution) -> Binding: ...

    def stopped(self, binding: Binding) -> EmptyObservation: ...


class BoardSync:
    """Keeps the local ledger and the board's record of this box in lockstep.

    Any disagreement freezes new starts rather than resetting either side: a missing, rolled-back or foreign
    journal must never be accepted as fresh.
    """

    def __init__(
        self,
        box: str,
        ledger: Ledger,
        board: VerifierBoard,
        inspector: BindingInspector,
        journal: LocalJournal,
    ) -> None:
        self._box = box
        self._ledger = ledger
        self._board = board
        self._inspector = inspector
        self._journal = journal
        self.frozen: str | None = "not_reconciled"
        self._lock = threading.RLock()

    def reconcile(self) -> str | None:
        with self._lock:
            return self._reconcile_locked()

    def _reconcile_locked(self) -> str | None:
        try:
            self.frozen = self._reconcile()
        except CaptureRefused as error:
            self.frozen = f"capture_refused:{error}"
        except (SyncError, LedgerError) as error:
            self.frozen = f"reconcile_failed:{error}"
        if self.frozen is not None:
            logger.error("night verifier frozen: %s", self.frozen)
        return self.frozen

    def requested_stops(self) -> frozenset[str]:
        """The runs the board asks this box to stop. Unreadable or foreign state asks for nothing, and a
        request never releases anything by itself: the stop path and settlement still need their proofs.

        The same fresh state is held to reconciliation: any disagreement with the durable local state runs
        the full reconcile, which freezes new starts on a run the journal does not know.
        """
        with self._lock:
            try:
                fresh = self._board.state()
            except SyncError as error:
                logger.warning("night stop requests unreadable: %s", error)
                return frozenset()
            if fresh.box != self._box:
                self.frozen = "wrong_box"
                return frozenset()
            if fresh.journal_id != self._ledger.identity().journal_id:
                self.frozen = "journal_identity_mismatch"
                return frozenset()
            local_runs = {record.run_id for record in self._journal.active()}
            if (
                fresh.counter != self._ledger.identity().acked_counter
                or self._ledger.unacknowledged()
                or not fresh.active_run_ids <= local_runs
            ):
                self._reconcile_locked()
            return fresh.stop_requested & fresh.active_run_ids & local_runs

    def bind(self, *, run_id: str, fence: str, uid: int, boot_id: str) -> None:
        """Serialized with reconcile and settle: the board counter allows one mutation in flight at a time."""
        with self._lock:
            self._bind_locked(run_id=run_id, fence=fence, uid=uid, boot_id=boot_id)

    def _bind_locked(self, *, run_id: str, fence: str, uid: int, boot_id: str) -> None:
        if self.frozen is not None:
            raise SyncError(f"frozen:{self.frozen}")
        try:
            binding = self._inspector.capture(run_id=run_id, uid=uid, boot_id=boot_id, execution=ROOT_EXEC)
            self._ledger.record_binding(
                run_id, uid=uid, boot_id=boot_id, invocation_id=binding.invocation_id, cgroup=binding.cgroup
            )
        except (InspectionError, LedgerError) as error:
            raise SyncError(f"binding:{error}") from None
        journal_id = self._ledger.identity().journal_id
        if journal_id is None:
            raise SyncError("not_bootstrapped")
        receipt_id = str(uuid4())
        self._deliver(
            Pending(
                receipt_id,
                "binding",
                run_id,
                {
                    "fence": fence,
                    "journal_id": journal_id,
                    "counter": self._ledger.identity().acked_counter,
                    "receipt_id": receipt_id,
                    "boot_id": boot_id,
                    "invocation_id": binding.invocation_id,
                    "cgroup": binding.cgroup,
                },
            )
        )

    def capture(self, record: Record, policy: NightPolicy, cutoff_utc: float) -> CapturedAssignment:
        """The run's held wakes, captured once and turned into an immutable assignment under `policy`.

        An acknowledged capture is returned as stored, never captured again or rebuilt; a receipt whose reply
        was lost is resent unchanged and gets the server's identical snapshot. The result is data only: it
        neither authorizes a launch nor acknowledges a wake, and None as its assignment means launch nothing.
        """
        with self._lock:
            if self.frozen is not None:
                raise SyncError(f"frozen:{self.frozen}")
            existing = self._ledger.captured(record.run_id)
            if isinstance(existing, CapturedAssignment):
                return existing
            if existing is None:
                recorded = self._ledger.binding(record.run_id)
                identity = self._ledger.identity()
                if recorded is None or not recorded.server_acked or identity.journal_id is None:
                    raise SyncError("not_bound")
                receipt_id = str(uuid4())
                existing = Pending(
                    receipt_id,
                    "capture",
                    record.run_id,
                    {
                        "fence": record.fence,
                        "journal_id": identity.journal_id,
                        "counter": identity.acked_counter,
                        "receipt_id": receipt_id,
                        "boot_id": recorded.boot_id,
                        "invocation_id": recorded.invocation_id,
                        "cgroup": recorded.cgroup,
                    },
                )
                self._ledger.persist_capture(existing, CaptureContext(record.principal, cutoff_utc, policy))
            try:
                self._send_and_acknowledge(existing)
            except CaptureRefused as error:
                self.frozen = f"capture_refused:{error}"
                raise
            stored = self._ledger.captured(record.run_id)
            if not isinstance(stored, CapturedAssignment):
                raise SyncError("capture_not_stored")
            return stored

    def settle(self, record: Record, reason: str) -> bool:
        with self._lock:
            return self._settle_locked(record, reason)

    def _settle_locked(self, record: Record, reason: str) -> bool:
        """Reports a proven-empty run to the board and frees its local slot only once the board agrees.

        Runtime is always reported unknown (a full charge): the unit's monotonic timestamps exclude suspend,
        which the budget counts. Any doubt, at any step, keeps the slot.
        """
        if self.frozen is not None or not record.launch_claimed:
            return False
        recorded = self._ledger.binding(record.run_id)
        if recorded is None or not recorded.server_acked:
            return False
        binding = Binding(record.run_id, recorded.uid, recorded.boot_id, recorded.invocation_id, ROOT_EXEC)
        try:
            self._inspector.stopped(binding)
            self._report(record, binding, recorded.cgroup, reason)
            if not self._board_released(record.run_id):
                return False
            self._journal.release_settled(
                record.run_id, record.fence, observation=self._inspector.stopped(binding)
            )
        except (InspectionError, SyncError, LedgerError, StateError) as error:
            logger.warning("night run %s stays held: %s", record.run_id, error)
            return False
        return True

    def _report(self, record: Record, binding: Binding, cgroup: str, reason: str) -> None:
        existing = self._ledger.evidence(record.run_id)
        if existing is not None:
            pending, acked = existing
            if not acked:
                self._send_and_acknowledge(pending)
            return
        identity = self._ledger.identity()
        if identity.journal_id is None:
            raise SyncError("not_bootstrapped")
        receipt_id = str(uuid4())
        self._deliver(
            Pending(
                receipt_id,
                "evidence",
                record.run_id,
                {
                    "fence": record.fence,
                    "journal_id": identity.journal_id,
                    "counter": identity.acked_counter,
                    "receipt_id": receipt_id,
                    "boot_id": binding.boot_id,
                    "invocation_id": binding.invocation_id,
                    "cgroup": cgroup,
                    "outcome": "empty",
                    "runtime_seconds": None,
                    "reason": reason,
                },
            )
        )

    def _board_released(self, run_id: str) -> bool:
        fresh = self._board.state()
        identity = self._ledger.identity()
        return (
            fresh.box == self._box
            and fresh.journal_id == identity.journal_id
            and fresh.counter == identity.acked_counter
            and not self._ledger.unacknowledged()
            and run_id not in fresh.active_run_ids
        )

    def _reconcile(self) -> str | None:
        remote = self._board.state()
        if remote.box != self._box:
            return "wrong_box"
        local = self._ledger.identity()
        pending = self._ledger.unacknowledged()
        if local.journal_id is None:
            bootstrap = _pending_bootstrap(pending)
            if bootstrap is None:
                if remote.journal_id is not None or remote.active_run_ids or self._journal.active():
                    return "journal_identity_mismatch"
                receipt_id = str(uuid4())
                bootstrap = Pending(
                    receipt_id, "bootstrap", None, {"journal_id": str(uuid4()), "receipt_id": receipt_id}
                )
                self._ledger.persist(bootstrap)
                pending = (bootstrap,)
            elif remote.journal_id not in (None, bootstrap.payload["journal_id"]):
                return "journal_identity_mismatch"
        elif remote.journal_id != local.journal_id:
            return "journal_identity_mismatch"
        for each in pending:
            self._send_and_acknowledge(each)
        fresh = self._board.state()
        if fresh.journal_id != self._ledger.identity().journal_id:
            return "journal_identity_mismatch"
        if fresh.counter != self._ledger.identity().acked_counter:
            return "counter_mismatch"
        local_runs = {record.run_id for record in self._journal.active()}
        if not fresh.active_run_ids <= local_runs:
            return "missing_local_run"
        return None

    def _deliver(self, pending: Pending) -> None:
        self._ledger.persist(pending)
        self._send_and_acknowledge(pending)

    def _send_and_acknowledge(self, pending: Pending) -> None:
        if pending.action == "bootstrap":
            ack = self._board.bootstrap(pending.payload)
        elif pending.action == "binding" and pending.run_id is not None:
            ack = self._board.binding(pending.run_id, pending.payload)
        elif pending.action == "evidence" and pending.run_id is not None:
            ack = self._board.evidence(pending.run_id, pending.payload)
        elif pending.action == "capture" and pending.run_id is not None:
            reply = self._board.capture(pending.run_id, pending.payload)
            derived = _derive(pending, reply, self._ledger.capture_context(pending.receipt_id))
            self._ledger.acknowledge(
                pending.receipt_id,
                journal_id=reply.ack.journal_id,
                counter=reply.ack.counter,
                capture=derived,
            )
            return
        else:
            raise SyncError("malformed_pending_receipt")
        self._ledger.acknowledge(pending.receipt_id, journal_id=ack.journal_id, counter=ack.counter)


def _pending_bootstrap(pending: Sequence[Pending]) -> Pending | None:
    return next((each for each in pending if each.action == "bootstrap"), None)


def _derive(pending: Pending, reply: CaptureReply, context: CaptureContext) -> tuple[str, bytes | None]:
    """Checks the reply against the receipt and builds the assignment from the receipt's stored choices.

    A reply that disagrees is refused before anything is stored; the server already counted it, so the box
    freezes rather than act on data it cannot vouch for, and only a validated replay can recover it.
    """
    snapshot = reply.snapshot
    expected_counter = pending.payload.get("counter")
    journal_id = pending.payload.get("journal_id")
    if (
        type(expected_counter) is not int
        or not isinstance(journal_id, str)
        or reply.ack != Ack(journal_id, expected_counter + 1)
        or snapshot.get("journal_id") != journal_id
        or snapshot.get("counter") != expected_counter + 1
    ):
        raise CaptureRefused("capture_counter_mismatch")
    deadline = _finite_positive(snapshot.get("deadline"))
    cutoff = _finite_positive(context.cutoff_utc)
    if (
        pending.run_id is None
        or snapshot.get("run_id") != pending.run_id
        or snapshot.get("capture_id") != pending.receipt_id
        or snapshot.get("principal") != context.principal
        or deadline is None
        or cutoff is None
    ):
        raise CaptureRefused("capture_reply_mismatch")
    try:
        wakes = parse_wakes(snapshot.get("wakes"))
        assignment = build_assignment(
            run_id=pending.run_id,
            principal=context.principal,
            wakes=wakes,
            policy=context.policy,
            deadline_utc=min(deadline, cutoff),
        )
        encoded = None if assignment is None else encode(assignment)
        stored = json.dumps(dict(snapshot), sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (AssignmentError, ValueError, TypeError, RecursionError) as error:
        raise CaptureRefused(f"capture_unusable:{type(error).__name__}") from None
    return stored, encoded


def _finite_positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number > 0 else None
