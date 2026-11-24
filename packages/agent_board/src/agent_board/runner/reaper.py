import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import NoReturn

from agent_board.runner.activity import MAX_RUN_SECONDS
from agent_board.runner.happier import Happier, HappierError
from agent_board.runner.sessions import SessionPool, UnreadableMap

ORPHAN_GRACE_SECONDS = MAX_RUN_SECONDS
ORPHAN_HARD_LIMIT_SECONDS = 4 * MAX_RUN_SECONDS
REAP_INTERVAL_SECONDS = 300
_DAEMON_STARTED = "daemon"
logger = logging.getLogger(__name__)


class OrphanReaper:
    """Ends board daemon sessions that no kind's session map records any more.

    The pools forget a session the relay shows inactive, and archiving such a session does not stop the
    daemon's child, so its host and CLI would run until the box runs out of memory. An orphan is stopped
    once it has stayed one for `ORPHAN_GRACE_SECONDS` and is idle, or for `ORPHAN_HARD_LIMIT_SECONDS`.
    The grace also covers a session that is being opened and is not in its map yet. A sweep reads every
    served kind's map first and does nothing if one is unreadable. A stopped orphan stays watched until
    the daemon no longer lists it, so a stop that did not end the child is retried and reported.
    """

    def __init__(
        self, happier: Happier, pools: Sequence[SessionPool], clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._happier = happier
        self._pools = pools
        self._clock = clock
        self._first_seen: dict[str, float] = {}
        self._stopped: set[str] = set()
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
                children = self._happier.children()
            except HappierError as error:
                logger.warning("could not list the daemon's sessions: %s", error)
                return
            try:
                recorded = set().union(*(pool.recorded() for pool in self._pools))
            except UnreadableMap as error:
                logger.warning("no orphan sweep while a session map is unreadable: %s", error)
                return
            orphans = {
                child.session_id
                for child in children
                if child.started_by == _DAEMON_STARTED and child.session_id not in recorded
            }
            now = self._clock()
            self._first_seen = {orphan: self._first_seen.get(orphan, now) for orphan in orphans}
            self._stopped &= orphans
            for orphan, since in sorted(self._first_seen.items()):
                if self._due(orphan, now - since):
                    self._stop(orphan, now - since)

    def _due(self, session_id: str, age: float) -> bool:
        if age >= ORPHAN_HARD_LIMIT_SECONDS:
            return True
        if age < ORPHAN_GRACE_SECONDS:
            return False
        try:
            return self._happier.idle(session_id)
        except HappierError as error:
            logger.warning("could not tell whether orphaned session %s is idle: %s", session_id, error)
            return False

    def _stop(self, session_id: str, age: float) -> None:
        if session_id in self._stopped:
            logger.warning("orphaned session %s is still a daemon child after it was stopped", session_id)
        if not self._happier.stop_child(session_id):
            logger.warning("the daemon did not stop orphaned session %s", session_id)
            return
        self._stopped.add(session_id)
        try:
            self._happier.archive(session_id)
        except HappierError as error:
            logger.warning("stopped orphaned session %s but could not archive it: %s", session_id, error)
        logger.info("stopped orphaned session %s after %ds unrecorded", session_id, age)
