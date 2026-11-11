import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agent_board.night.ledger import Ledger
from agent_board.night.systemd import GATE_TIMEOUT_SECONDS
from agent_board.night_state import LocalJournal

POLL_SECONDS = 0.5
_MARGIN_SECONDS = 5
_INVOCATION = re.compile(r"[0-9a-f]{32}")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardState:
    active: bool
    invocation_id: str


def open_gate(
    run_id: str,
    *,
    ledger: Ledger,
    journal: LocalJournal,
    environ: Mapping[str, str],
    boot_id: Callable[[], str],
    guard: Callable[[], GuardState],
    boottime: Callable[[], float] = lambda: time.clock_gettime(time.CLOCK_BOOTTIME),
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Runs as root inside the run's own unit before native exec; True only once the board and the guard agree.

    The unit's own invocation id must be the one the board recorded, so the gate cannot be satisfied for a
    different or replaced unit, and the guard must be the one running now, not an earlier arm.
    """
    own = environ.get("INVOCATION_ID", "")
    if _INVOCATION.fullmatch(own) is None:
        logger.error("gate for %s: no valid INVOCATION_ID", run_id)
        return False
    give_up = boottime() + GATE_TIMEOUT_SECONDS - _MARGIN_SECONDS
    while boottime() < give_up:
        if _ready(run_id, own, ledger=ledger, journal=journal, boot_id=boot_id(), guard=guard()):
            return True
        sleep(POLL_SECONDS)
    logger.error("gate for %s timed out; the run will not execute", run_id)
    return False


def _ready(
    run_id: str, own: str, *, ledger: Ledger, journal: LocalJournal, boot_id: str, guard: GuardState
) -> bool:
    record = next((each for each in journal.active() if each.run_id == run_id), None)
    if record is None or record.state != "starting" or record.boot_id != boot_id:
        return False
    binding = ledger.binding(run_id)
    if binding is None or binding.invocation_id != own or binding.boot_id != boot_id:
        return False
    if not guard.active or _INVOCATION.fullmatch(guard.invocation_id) is None:
        return False
    return ledger.ready(
        run_id,
        boot_id=boot_id,
        guard_invocation_id=guard.invocation_id,
        deadline_boottime=record.deadline_boottime,
        deadline_utc=record.deadline_utc,
    )
