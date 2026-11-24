import logging
import sqlite3
from functools import partial

from board.constants import HUMAN
from board.db import Database
from board.forum.fanout import WakeRequest
from board.wakes import self_wakes
from board.wakes.bus import WakeBus, store_in_transaction

TICK_SECONDS = 60
TIMER = "timer"
QUIET_TICKET = "quiet_ticket"
QUIET_SECONDS = 45 * 60

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
    for _, ticket_id in await db.transact(partial(_quiet_tickets, now=now)):
        try:
            woken = await db.transact(partial(_nudge, ticket_id=ticket_id, now=now))
        except Exception:
            logger.exception("quiet ticket wake for ticket %d failed; retrying next tick", ticket_id)
            continue
        if woken is not None:
            bus.arrived(woken)
    for _, wake_id in await db.transact(partial(self_wakes.due, now=now)):
        try:
            woken = await db.transact(partial(self_wakes.deliver, wake_id=wake_id, now=now))
        except Exception:
            logger.exception("self wake %d failed; retrying next tick", wake_id)
            continue
        if woken is not None:
            bus.arrived(woken)


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


def _nudge(conn: sqlite3.Connection, ticket_id: int, now: float) -> str | None:
    """Checks the ticket again, stores its wake and stamps it in one transaction; the woken principal, or None."""
    for request, _ in _quiet_tickets(conn, now, ticket_id):
        store_in_transaction(conn, request)
        _stamp_quiet(conn, ticket_id, now)
        return request.principal
    return None


def _quiet_tickets(
    conn: sqlite3.Connection, now: float, only: int | None = None
) -> list[tuple[WakeRequest, int]]:
    """Doing tickets whose agent assignee has neither posted on nor touched them for `QUIET_SECONDS`.

    A ticket is skipped while its previous nudge is still unacknowledged, and while its assignee's night
    holds budgeted wakes, since the night run already continues doing tickets.
    """
    rows = conn.execute(
        "SELECT t.id, t.title, t.assignee, t.thread_id, COALESCE(th.topic_id, th.id) AS topic_id FROM tickets t "
        "JOIN threads th ON th.id = t.thread_id "
        "WHERE t.\"column\" = 'doing' AND t.assignee IS NOT NULL AND t.assignee != ? "
        "AND MAX(t.updated_at, COALESCE(t.quiet_wake_at, 0), COALESCE("
        "(SELECT MAX(p.created_at) FROM posts p WHERE p.thread_id = t.thread_id AND p.author = t.assignee), 0)"
        ") <= ? "
        "AND NOT EXISTS (SELECT 1 FROM wakes w WHERE w.principal = t.assignee AND w.reason = ? "
        "AND w.source_ref = 'ticket:' || t.id AND w.acked_at IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM night_routing_days d WHERE d.principal = t.assignee "
        "AND d.starts_at <= ? AND ? < d.ends_at) "
        "AND (? IS NULL OR t.id = ?) "
        "ORDER BY t.id",
        (HUMAN, now - QUIET_SECONDS, QUIET_TICKET, now, now, only, only),
    )
    return [
        (
            WakeRequest(
                row["assignee"],
                QUIET_TICKET,
                f"ticket:{row['id']}",
                f'your ticket #{row["id"]} "{row["title"]}" is in doing with no post from you for '
                f"{QUIET_SECONDS // 60} min; do the next piece of work on it now, or move it out of doing "
                "and name what it waits for",
                thread_id=row["thread_id"],
                topic_id=row["topic_id"],
            ),
            row["id"],
        )
        for row in rows
    ]


def _stamp_quiet(conn: sqlite3.Connection, ticket_id: int, now: float) -> None:
    conn.execute("UPDATE tickets SET quiet_wake_at = ? WHERE id = ?", (now, ticket_id))
