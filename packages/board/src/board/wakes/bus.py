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


class WakeBus:
    def __init__(self, db: Database, lease_seconds: int, human_digest_seconds: int = 0) -> None:
        self._db = db
        self._lease_seconds = lease_seconds
        self._human_digest_seconds = human_digest_seconds
        self._arrivals: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def emit(self, request: WakeRequest) -> None:
        def store(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO wakes (principal, reason, source_ref, summary, created_at) VALUES (?, ?, ?, ?, ?)",
                (request.principal, request.reason, request.source_ref, request.summary, time.time()),
            )

        await self._db.transact(store)
        self._arrivals[request.principal].set()

    async def lease(self, principal: str, limit: int = LEASE_BATCH) -> list[Wake]:
        now = time.time()
        leased_until = now + self._lease_seconds

        def take(conn: sqlite3.Connection) -> list[Wake]:
            if principal == HUMAN and not _digest_due(conn, now, self._human_digest_seconds):
                return []
            rows = conn.execute(
                "SELECT id, principal, reason, source_ref, summary, created_at FROM wakes "
                "WHERE principal = ? AND acked_at IS NULL AND (leased_until IS NULL OR leased_until < ?) "
                "ORDER BY id LIMIT ?",
                (principal, now, limit),
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
                "UPDATE wakes SET acked_at = ? WHERE id = ? AND principal = ? AND acked_at IS NULL",
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


def _digest_due(conn: sqlite3.Connection, now: float, digest_seconds: int) -> bool:
    if digest_seconds <= 0:
        return True
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
    )
