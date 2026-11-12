import asyncio
import contextlib
import sqlite3
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from board.constants import HUMAN
from board.db import Database
from board.forum.fanout import WakeRequest
from board.night import hold_wakes_in_transaction
from board.wakes.origins import classify_origin

LEASE_BATCH = 20
DISCONNECT_CHECK_SECONDS = 1.0

type Gone = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class Wake:
    id: int
    principal: str
    reason: str
    source_ref: str | None
    summary: str
    created_at: float
    leased_until: float
    thread_id: int | None
    topic_id: int | None


class WakeBus:
    def __init__(self, db: Database, lease_seconds: int, human_digest_seconds: int = 0) -> None:
        self._db = db
        self._lease_seconds = lease_seconds
        self._human_digest_seconds = human_digest_seconds
        self._arrivals: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def emit(self, request: WakeRequest) -> None:
        def store(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO wakes (principal, reason, source_ref, summary, created_at, thread_id, topic_id, origin_kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.principal,
                    request.reason,
                    request.source_ref,
                    request.summary,
                    time.time(),
                    request.thread_id,
                    request.topic_id,
                    classify_origin(conn, request),
                ),
            )
            hold_wakes_in_transaction(conn, time.time(), request.principal)
            _request_human_preemption(conn, request.principal)

        await self._db.transact(store)
        self._arrivals[request.principal].set()

    async def lease(self, principal: str, limit: int = LEASE_BATCH) -> list[Wake]:
        now = time.time()
        leased_until = now + self._lease_seconds

        def take(conn: sqlite3.Connection) -> list[Wake]:
            hold_wakes_in_transaction(conn, now, principal)
            _request_human_preemption(conn, principal)
            batch_limit = limit
            if principal == HUMAN and self._human_digest_seconds > 0:
                if not _digest_due(conn, now, self._human_digest_seconds):
                    return []
                # A matured digest goes to one relay whole; a capped batch would leave the rest for a
                # second relay, which would send a second push for the same burst.
                batch_limit = -1
            rows = conn.execute(
                "SELECT id, principal, reason, source_ref, summary, created_at, thread_id, topic_id FROM wakes "
                "WHERE principal = ? AND acked_at IS NULL AND (leased_until IS NULL OR leased_until < ?) "
                "AND NOT EXISTS (SELECT 1 FROM night_held_wakes h WHERE h.wake_id = wakes.id) "
                "AND NOT EXISTS (SELECT 1 FROM night_runs r WHERE r.principal = wakes.principal "
                "AND r.state != 'finished') ORDER BY id LIMIT ?",
                (principal, now, batch_limit),
            ).fetchall()
            conn.executemany(
                "UPDATE wakes SET leased_until = ? WHERE id = ?", [(leased_until, row["id"]) for row in rows]
            )
            return [_wake(row, leased_until) for row in rows]

        return await self._db.transact(take)

    async def ack(self, principal: str, ids: Iterable[int]) -> int:
        now = time.time()

        def stamp(conn: sqlite3.Connection) -> int:
            cursor = conn.executemany(
                "UPDATE wakes SET acked_at = ? WHERE id = ? AND principal = ? AND acked_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM night_held_wakes h WHERE h.wake_id = wakes.id)",
                [(now, wake_id, principal) for wake_id in ids],
            )
            return cursor.rowcount

        return await self._db.transact(stamp)

    async def wait_then_lease(self, principal: str, timeout: float, gone: Gone | None = None) -> list[Wake]:
        arrival = self._arrivals[principal]
        deadline = time.monotonic() + timeout
        while True:
            # Clearing before the lease, not after, means a wake committed between the lease's SELECT
            # and the wait still sets the event, so it is never missed until the deadline.
            arrival.clear()
            leased = await self.lease(principal)
            remaining = deadline - time.monotonic()
            if leased or remaining <= 0:
                return leased
            while not arrival.is_set():
                if gone is not None and await gone():
                    return []
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(arrival.wait(), min(remaining, DISCONNECT_CHECK_SECONDS))


def _request_human_preemption(conn: sqlite3.Connection, principal: str) -> None:
    conn.execute(
        "UPDATE night_runs SET state = 'stopping' WHERE principal = ? "
        "AND state IN ('admitted', 'running') AND EXISTS "
        "(SELECT 1 FROM wakes WHERE principal = ? AND acked_at IS NULL AND origin_kind = 'human')",
        (principal, principal),
    )


def _digest_due(conn: sqlite3.Connection, now: float, digest_seconds: int) -> bool:
    (oldest,) = conn.execute(
        "SELECT MIN(created_at) FROM wakes "
        "WHERE principal = ? AND acked_at IS NULL AND (leased_until IS NULL OR leased_until < ?)",
        (HUMAN, now),
    ).fetchone()
    return oldest is not None and oldest <= now - digest_seconds


def _wake(row: sqlite3.Row, leased_until: float) -> Wake:
    return Wake(
        row["id"],
        row["principal"],
        row["reason"],
        row["source_ref"],
        row["summary"],
        row["created_at"],
        leased_until,
        row["thread_id"],
        row["topic_id"],
    )
