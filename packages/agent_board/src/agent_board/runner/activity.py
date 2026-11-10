import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol

from agent_board.client import BoardError, BoardUnreachable
from agent_board.runner.happier import HappierError

REPORT_PATH = "/api/activity"
PROBE_SECONDS = 20
LONG_RUN_PROBE_SECONDS = 60
LONG_RUN_SECONDS = 300
MAX_RUN_SECONDS = 2 * 3600
MAX_PROBE_ERRORS = 3
ROUTE_RETRY_SECONDS = 3600
TTL_SECONDS = 90
REFRESH_SECONDS = 30
SETTLE_SECONDS = 15
_IDLE_TIMEOUT = 1
_MISSING_ROUTE = (404, 405)
_THREAD = re.compile(r"\S+ (?:mentioned you|posted) in thread (\d+)\b")
logger = logging.getLogger(__name__)


class _Board(Protocol):
    def post(self, path: str, body: dict[str, Any]) -> Any: ...


class _Sessions(Protocol):
    def idle(self, session_id: str, timeout: int = 3) -> bool: ...


@dataclass
class _Run:
    since: float
    delivered_at: float
    threads: set[int] = field(default_factory=set)
    probed_at: float | None = None
    errors: int = 0

    def probe_due(self, now: float) -> bool:
        if now - self.delivered_at < SETTLE_SECONDS:
            return False
        interval = PROBE_SECONDS if now - self.since < LONG_RUN_SECONDS else LONG_RUN_PROBE_SECONDS
        return self.probed_at is None or now - self.probed_at >= interval


class ActivityReporter:
    """Tells the board which of one agent's sessions are mid-turn, for the human's agents page.

    A delivery marks its session running at once. Only `idle` can see a turn end, and a just-sent prompt
    may not have started its turn, so a session is probed only after `SETTLE_SECONDS`. A run whose probes
    keep failing, or that outlives `MAX_RUN_SECONDS`, is dropped so a vanished session cannot show as
    running forever.
    """

    def __init__(self, board: _Board, sessions: _Sessions, clock: Callable[[], float] = time.time) -> None:
        self._board = board
        self._sessions = sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}
        self._dirty = False
        self._reported_at: float | None = None
        self._paused_until: float | None = None

    def delivered(self, session_id: str, threads: Iterable[int]) -> None:
        now = self._clock()
        with self._lock:
            run = self._runs.setdefault(session_id, _Run(since=now, delivered_at=now))
            run.delivered_at = now
            run.errors = 0
            run.threads.update(threads)
            self._dirty = True

    def probe(self) -> None:
        now = self._clock()
        if self._paused_until is not None and now < self._paused_until:
            return
        for session_id in self._due(now):
            self._check(session_id, now)
        self._report_when_due()

    def serve(self, interval: float = PROBE_SECONDS) -> NoReturn:
        while True:
            try:
                self.probe()
            except Exception:
                logger.exception("activity report failed; retrying in %ds", interval)
            time.sleep(interval)

    def _due(self, now: float) -> list[str]:
        with self._lock:
            return [session_id for session_id, run in self._runs.items() if run.probe_due(now)]

    def _check(self, session_id: str, now: float) -> None:
        try:
            idle = self._sessions.idle(session_id, timeout=_IDLE_TIMEOUT)
            failed = False
        except HappierError as error:
            logger.warning("could not tell whether session %s is idle: %s", session_id, error)
            idle, failed = False, True
        with self._lock:
            run = self._runs.get(session_id)
            if run is None:
                return
            run.probed_at = now
            run.errors = run.errors + 1 if failed else 0
            if run.errors >= MAX_PROBE_ERRORS:
                logger.warning(
                    "session %s failed %d idle probes; no longer shown running", session_id, run.errors
                )
                idle = True
            elif now - run.since >= MAX_RUN_SECONDS:
                logger.warning(
                    "session %s has run over %ds; no longer shown running", session_id, MAX_RUN_SECONDS
                )
                idle = True
            if idle:
                del self._runs[session_id]
                self._dirty = True

    def _report_when_due(self) -> None:
        now = self._clock()
        with self._lock:
            refresh_due = (
                bool(self._runs)
                and self._reported_at is not None
                and now - self._reported_at >= REFRESH_SECONDS
            )
            if not (self._dirty or refresh_due):
                return
            body = {
                "running": [
                    {"threads": sorted(run.threads), "since": run.since}
                    for run in sorted(self._runs.values(), key=lambda run: run.since)
                ],
                "ttl_seconds": TTL_SECONDS,
            }
        try:
            self._board.post(REPORT_PATH, body)
        except BoardError as error:
            if error.status in _MISSING_ROUTE:
                self._paused_until = now + ROUTE_RETRY_SECONDS
                logger.warning(
                    "the board answered %d for %s; retrying in %ds",
                    error.status,
                    REPORT_PATH,
                    ROUTE_RETRY_SECONDS,
                )
                return
            logger.warning("activity report refused: %s", error)
            return
        except BoardUnreachable as error:
            logger.warning("activity report not sent: %s", error)
            return
        with self._lock:
            self._dirty = False
            self._reported_at = now


def threads_of(wakes: Sequence[dict[str, Any]]) -> tuple[int, ...]:
    found: list[int] = []
    for wake in wakes:
        thread = wake.get("thread_id")
        if thread is None and (match := _THREAD.match(str(wake.get("summary", "")))) is not None:
            thread = int(match[1])
        if isinstance(thread, int) and thread not in found:
            found.append(thread)
    return tuple(found)
