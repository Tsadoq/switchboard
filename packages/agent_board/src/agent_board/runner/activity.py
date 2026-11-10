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


class ActivityReporter:
    """Tells the board which of one agent's sessions are mid-turn, for the human's agents page.

    A delivery marks its session running at once. Only `idle` can see a turn end, and a just-sent prompt
    may not have started its turn, so a session is probed only after `SETTLE_SECONDS`.
    """

    def __init__(self, board: _Board, sessions: _Sessions, clock: Callable[[], float] = time.time) -> None:
        self._board = board
        self._sessions = sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}
        self._dirty = False
        self._reported_at: float | None = None
        self._enabled = True

    def delivered(self, session_id: str, threads: Iterable[int]) -> None:
        now = self._clock()
        with self._lock:
            run = self._runs.setdefault(session_id, _Run(since=now, delivered_at=now))
            run.delivered_at = now
            run.threads.update(threads)
            self._dirty = True

    def probe(self) -> None:
        if not self._enabled:
            return
        for session_id in self._settled():
            if self._idle(session_id):
                with self._lock:
                    self._runs.pop(session_id, None)
                    self._dirty = True
        self._report_when_due()

    def serve(self, interval: float = PROBE_SECONDS) -> NoReturn:
        while True:
            try:
                self.probe()
            except Exception:
                logger.exception("activity report failed; retrying in %ds", interval)
            time.sleep(interval)

    def _settled(self) -> list[str]:
        now = self._clock()
        with self._lock:
            return [sid for sid, run in self._runs.items() if now - run.delivered_at >= SETTLE_SECONDS]

    def _idle(self, session_id: str) -> bool:
        try:
            return self._sessions.idle(session_id, timeout=_IDLE_TIMEOUT)
        except HappierError as error:
            logger.warning("could not tell whether session %s is idle: %s", session_id, error)
            return False

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
                self._enabled = False
                logger.warning("the board has no %s route yet; not reporting running sessions", REPORT_PATH)
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
