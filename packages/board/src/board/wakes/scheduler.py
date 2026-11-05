import logging
import sqlite3
from functools import partial

from board.db import Database
from board.forum.fanout import WakeRequest
from board.wakes.bus import WakeBus

TICK_SECONDS = 60
TIMER = "timer"

logger = logging.getLogger(__name__)


async def tick(db: Database, bus: WakeBus, now: float) -> None:
    for principal, every_seconds in await db.transact(partial(_due_timers, now=now)):
        try:
            await bus.emit(
                WakeRequest(
                    principal, TIMER, f"profile:{principal}", f"scheduled wake, every {every_seconds} s"
                )
            )
            await db.transact(partial(_stamp_timer, principal=principal, now=now))
        except Exception:
            logger.exception("timer wake for %s failed; retrying next tick", principal)


def _due_timers(conn: sqlite3.Connection, now: float) -> list[tuple[str, int]]:
    conn.execute(
        "UPDATE profiles SET last_timer_wake_at = ? "
        "WHERE wake_every_seconds IS NOT NULL AND last_timer_wake_at IS NULL",
        (now,),
    )
    rows = conn.execute(
        "SELECT principal, wake_every_seconds FROM profiles "
        "WHERE wake_every_seconds IS NOT NULL AND last_timer_wake_at + wake_every_seconds <= ? "
        "ORDER BY principal",
        (now,),
    )
    return [(row["principal"], row["wake_every_seconds"]) for row in rows]


def _stamp_timer(conn: sqlite3.Connection, principal: str, now: float) -> None:
    conn.execute("UPDATE profiles SET last_timer_wake_at = ? WHERE principal = ?", (now, principal))
