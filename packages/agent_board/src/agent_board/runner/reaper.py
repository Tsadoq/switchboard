import json
import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import NoReturn

from agent_board.runner.activity import MAX_RUN_SECONDS
from agent_board.runner.happier import Happier, HappierError, StopOutcome
from agent_board.runner.sessions import SessionPool, UnreadableMap

ORPHAN_GRACE_SECONDS = MAX_RUN_SECONDS
ORPHAN_BUSY_ALERT_SECONDS = 4 * MAX_RUN_SECONDS
REAP_INTERVAL_SECONDS = 300
LEDGER_FILE = "runner-reaper.json"
PRESENT_AT_START = "present before the reaper first ran"
NOT_BOARD = "not opened by the board runner"
STOP_DISPATCHED = "stop sent, outcome not recorded"
STOP_REPORTED = "the daemon reported it stopped"
STOP_REFUSED = "the daemon refused to stop it"
STOP_UNCONFIRMED = "the daemon did not confirm the stop"
_OUTCOMES = {
    StopOutcome.STOPPED: STOP_REPORTED,
    StopOutcome.REFUSED: STOP_REFUSED,
    StopOutcome.UNCONFIRMED: STOP_UNCONFIRMED,
}
_DAEMON_STARTED = "daemon"
_LEDGER_VERSION = 1
logger = logging.getLogger(__name__)


class UnreadableLedger(Exception):
    pass


class OrphanReaper:
    """Ends board daemon sessions that no record of any served kind names any more.

    The pools forget a session the relay shows inactive, and archiving such a session does not stop the
    daemon's child, so its host and CLI would run until the box runs out of memory.

    An orphan is a child the daemon started that no served kind records, that the relay tags and places
    as a session one of the pools would open, that is not in the ledger, and that has stayed so for
    `ORPHAN_GRACE_SECONDS`. It is stopped only when the relay explicitly reports it idle, and only
    while every served kind's map lock is held and the maps, read again, still do not name it. A busy
    orphan, or one the relay no longer knows, is never stopped; a busy one is reported once past
    `ORPHAN_BUSY_ALERT_SECONDS`.

    The ledger is the durable record of every session the reaper must not stop, and it is never pruned.
    The first sweep with no ledger enters every orphan already present and stops nothing. Each decision
    is written before the next is made. A stop is entered before it is sent, so a failed write prevents
    it and an interrupted one is never repeated; every session gets at most one stop, whatever its
    outcome. A sweep does nothing while the ledger or any session record is unreadable. Only the
    session's owner or the human removes an entry, by editing the ledger.
    """

    def __init__(
        self,
        happier: Happier,
        pools: Sequence[SessionPool],
        ledger: Path,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._happier = happier
        self._pools = pools
        self._ledger = ledger
        self._clock = clock
        self._first_seen: dict[str, float] = {}
        self._stopped: set[str] = set()
        self._reported: set[str] = set()
        self._lock = threading.Lock()

    def serve(self) -> NoReturn:
        while True:
            try:
                self.sweep()
            except Exception:
                logger.exception("orphan sweep failed")
            time.sleep(REAP_INTERVAL_SECONDS)

    def sweep(self) -> None:
        with self._lock:
            try:
                entered = self._read_ledger()
                children = self._happier.children()
                recorded = self._recorded_now()
            except UnreadableLedger as error:
                logger.warning("no orphan sweep while the reaper ledger is unreadable: %s", error)
                return
            except HappierError as error:
                logger.warning("could not list the daemon's sessions: %s", error)
                return
            except UnreadableMap as error:
                logger.warning("no orphan sweep while a session map is unreadable: %s", error)
                return
            listed = {child.session_id for child in children}
            unrecorded = {
                child.session_id
                for child in children
                if child.started_by == _DAEMON_STARTED and child.session_id not in recorded
            }
            if entered is None:
                self._write_ledger(dict.fromkeys(sorted(unrecorded), PRESENT_AT_START))
                logger.info("reaper ledger created; %d existing orphans entered", len(unrecorded))
                return
            self._report_inventory(entered, listed)
            orphans = unrecorded - set(entered)
            now = self._clock()
            self._first_seen = {orphan: self._first_seen.get(orphan, now) for orphan in orphans}
            for orphan, since in sorted(self._first_seen.items()):
                if now - since >= ORPHAN_GRACE_SECONDS:
                    self._consider(orphan, now - since, entered)

    def _report_inventory(self, entered: dict[str, str], listed: set[str]) -> None:
        for gone in sorted(self._stopped - listed):
            logger.info("orphaned session %s is no longer listed by the daemon", gone)
        self._stopped &= listed
        for session_id in sorted(listed & set(entered)):
            if entered[session_id] != PRESENT_AT_START and session_id not in self._reported:
                self._reported.add(session_id)
                logger.warning(
                    "daemon session %s is still listed; ledger: %s", session_id, entered[session_id]
                )

    def _recorded_now(self) -> set[str]:
        with ExitStack() as stack:
            readers = [stack.enter_context(pool.held()) for pool in self._pools]
            return set().union(*(read() for read in readers))

    def _consider(self, session_id: str, age: float, entered: dict[str, str]) -> None:
        try:
            labels = self._happier.labels(session_id)
            idle = self._happier.confirmed_idle(session_id)
        except HappierError as error:
            logger.warning("could not inspect orphaned session %s: %s", session_id, error)
            return
        if labels is None or not any(pool.opened(*labels) for pool in self._pools):
            self._enter(entered, session_id, NOT_BOARD)
            logger.warning("daemon session %s is %s; entered in the ledger", session_id, NOT_BOARD)
            return
        if not idle:
            if age >= ORPHAN_BUSY_ALERT_SECONDS and session_id not in self._reported:
                self._reported.add(session_id)
                logger.warning("orphaned session %s has been busy for %ds; left running", session_id, age)
            return
        with ExitStack() as stack:
            readers = [stack.enter_context(pool.held()) for pool in self._pools]
            try:
                recorded = set().union(*(read() for read in readers))
            except UnreadableMap as error:
                logger.warning("orphaned session %s not stopped: %s", session_id, error)
                return
            if session_id in recorded:
                logger.info("session %s was recorded again before its stop; left running", session_id)
                del self._first_seen[session_id]
                return
            self._enter(entered, session_id, STOP_DISPATCHED)
            del self._first_seen[session_id]
            outcome = self._happier.stop_child(session_id)
        self._enter(entered, session_id, _OUTCOMES[outcome])
        if outcome is not StopOutcome.STOPPED:
            logger.warning("orphaned session %s: %s; entered in the ledger", session_id, _OUTCOMES[outcome])
            return
        self._stopped.add(session_id)
        try:
            self._happier.archive(session_id)
        except HappierError as error:
            logger.warning("could not archive stopped orphaned session %s: %s", session_id, error)
        logger.info("the daemon reported orphaned session %s stopped after %ds unrecorded", session_id, age)

    def _enter(self, entered: dict[str, str], session_id: str, why: str) -> None:
        self._write_ledger({**entered, session_id: why})
        entered[session_id] = why

    def _read_ledger(self) -> dict[str, str] | None:
        try:
            recorded = json.loads(self._ledger.read_text())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise UnreadableLedger(f"{self._ledger}: {error}") from None
        if not isinstance(recorded, dict) or recorded.get("v") != _LEDGER_VERSION:
            raise UnreadableLedger(f"{self._ledger}: unexpected shape")
        entered = recorded.get("protected")
        if not isinstance(entered, dict):
            raise UnreadableLedger(f"{self._ledger}: unexpected shape")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in entered.items()):
            raise UnreadableLedger(f"{self._ledger}: unexpected entry")
        return dict(entered)

    def _write_ledger(self, entered: dict[str, str]) -> None:
        staged = self._ledger.with_name(f"{self._ledger.name}.tmp")
        with open(staged, "w") as file:
            file.write(json.dumps({"v": _LEDGER_VERSION, "protected": entered}, indent=1, sort_keys=True))
            file.flush()
            os.fsync(file.fileno())
        os.replace(staged, self._ledger)
