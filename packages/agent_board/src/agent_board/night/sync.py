import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from agent_board.night.ledger import Ledger, LedgerError, Pending
from agent_board.night_inspection import Binding, InspectionError
from agent_board.night_state import LocalJournal

logger = logging.getLogger(__name__)


class SyncError(RuntimeError):
    pass


class BoardRefused(SyncError):
    """The board answered with a mismatch (409) or an authorization refusal (401)."""


class BoardUnavailable(SyncError):
    pass


@dataclass(frozen=True)
class BoardState:
    box: str
    journal_id: str | None
    counter: int
    active_run_ids: frozenset[str]


@dataclass(frozen=True)
class Ack:
    journal_id: str
    counter: int


class VerifierBoard(Protocol):
    """The verifier's signed /api/night/verifier actions; each raises BoardRefused or BoardUnavailable."""

    def state(self) -> BoardState: ...

    def bootstrap(self, payload: Mapping[str, Any]) -> Ack: ...

    def binding(self, run_id: str, payload: Mapping[str, Any]) -> Ack: ...

    def evidence(self, run_id: str, payload: Mapping[str, Any]) -> Ack: ...


class BindingInspector(Protocol):
    def capture(self, *, run_id: str, uid: int, boot_id: str) -> Binding: ...


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

    def reconcile(self) -> str | None:
        try:
            self.frozen = self._reconcile()
        except (SyncError, LedgerError) as error:
            self.frozen = f"reconcile_failed:{error}"
        if self.frozen is not None:
            logger.error("night verifier frozen: %s", self.frozen)
        return self.frozen

    def bind(self, *, run_id: str, fence: str, uid: int, boot_id: str) -> None:
        if self.frozen is not None:
            raise SyncError(f"frozen:{self.frozen}")
        try:
            binding = self._inspector.capture(run_id=run_id, uid=uid, boot_id=boot_id)
            self._ledger.record_binding(
                run_id, boot_id=boot_id, invocation_id=binding.invocation_id, cgroup=binding.cgroup
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
        else:
            raise SyncError("malformed_pending_receipt")
        self._ledger.acknowledge(pending.receipt_id, journal_id=ack.journal_id, counter=ack.counter)


def _pending_bootstrap(pending: Sequence[Pending]) -> Pending | None:
    return next((each for each in pending if each.action == "bootstrap"), None)
