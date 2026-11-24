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
STOP_REFUSED = "the daemon refused to stop it"
STILL_LISTED = "still listed after the daemon reported it stopped"
NOT_BOARD = "not opened by the board runner"
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
    as a session one of the pools would open, that is not in the protected ledger, and that has stayed
    so for `ORPHAN_GRACE_SECONDS`. It is stopped only when the relay explicitly reports it idle, and only
    while every served kind's map lock is held and the maps, read again, still do not name it. A busy
    orphan, or one the relay no longer knows, is never stopped; a busy one is reported once past
    `ORPHAN_BUSY_ALERT_SECONDS`.

    The ledger fails closed. The first sweep with no ledger protects every orphan already present and
    stops nothing. A refused stop, or a child still listed after a reported stop, is protected rather
    than retried. A sweep does nothing while the ledger or any session record is unreadable. Only the
    human or the session's owner clears a protected entry, by editing the ledger.
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
        self._busy_reported: set[str] = set()
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
                protected = self._read_ledger()
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
            for gone in sorted(self._stopped - listed):
                logger.info("orphaned session %s is no longer listed by the daemon", gone)
            if protected is None:
                self._write_ledger(dict.fromkeys(sorted(unrecorded), PRESENT_AT_START))
                logger.info("reaper ledger created; %d existing orphans protected", len(unrecorded))
                return
            pruned = {session_id: why for session_id, why in protected.items() if session_id in listed}
            for still in sorted(self._stopped & listed):
                logger.warning("orphaned session %s is %s; protected", still, STILL_LISTED)
                pruned[still] = STILL_LISTED
            self._stopped.clear()
            orphans = unrecorded - set(pruned)
            now = self._clock()
            self._first_seen = {orphan: self._first_seen.get(orphan, now) for orphan in orphans}
            self._busy_reported &= orphans
            for orphan, since in sorted(self._first_seen.items()):
                if now - since >= ORPHAN_GRACE_SECONDS:
                    self._consider(orphan, now - since, pruned)
            if pruned != protected:
                self._write_ledger(pruned)

    def _recorded_now(self) -> set[str]:
        with ExitStack() as stack:
            readers = [stack.enter_context(pool.held()) for pool in self._pools]
            return set().union(*(read() for read in readers))

    def _consider(self, session_id: str, age: float, protected: dict[str, str]) -> None:
        try:
            labels = self._happier.labels(session_id)
            idle = self._happier.confirmed_idle(session_id)
        except HappierError as error:
            logger.warning("could not inspect orphaned session %s: %s", session_id, error)
            return
        if labels is None or not any(pool.opened(*labels) for pool in self._pools):
            logger.warning("daemon session %s is %s; protected", session_id, NOT_BOARD)
            protected[session_id] = NOT_BOARD
            return
        if not idle:
            if age >= ORPHAN_BUSY_ALERT_SECONDS and session_id not in self._busy_reported:
                self._busy_reported.add(session_id)
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
            outcome = self._happier.stop_child(session_id)
        if outcome is StopOutcome.REFUSED:
            logger.warning("orphaned session %s: %s; protected", session_id, STOP_REFUSED)
            protected[session_id] = STOP_REFUSED
            return
        if outcome is StopOutcome.UNAVAILABLE:
            logger.warning("could not reach the daemon to stop orphaned session %s; will retry", session_id)
            return
        self._stopped.add(session_id)
        del self._first_seen[session_id]
        try:
            self._happier.archive(session_id)
        except HappierError as error:
            logger.warning("could not archive stopped orphaned session %s: %s", session_id, error)
        logger.info("the daemon reported orphaned session %s stopped after %ds unrecorded", session_id, age)

    def _read_ledger(self) -> dict[str, str] | None:
        try:
            recorded = json.loads(self._ledger.read_text())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise UnreadableLedger(f"{self._ledger}: {error}") from None
        if not isinstance(recorded, dict) or recorded.get("v") != _LEDGER_VERSION:
            raise UnreadableLedger(f"{self._ledger}: unexpected shape")
        protected = recorded.get("protected")
        if not isinstance(protected, dict):
            raise UnreadableLedger(f"{self._ledger}: unexpected shape")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in protected.items()):
            raise UnreadableLedger(f"{self._ledger}: unexpected entry")
        return dict(protected)

    def _write_ledger(self, protected: dict[str, str]) -> None:
        staged = self._ledger.with_name(f"{self._ledger.name}.tmp")
        staged.write_text(
            json.dumps({"v": _LEDGER_VERSION, "protected": protected}, indent=1, sort_keys=True)
        )
        os.replace(staged, self._ledger)
