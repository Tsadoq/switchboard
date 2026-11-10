import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from board.db import Database
from board.forum import mentions
from board.forum.actions import Forum, Refused
from board.forum.entities import AuthorKind, SubforumTarget, ThreadTarget
from board.identity.verifier import Verified
from board.tickets import COLUMNS

TICKETS_SUBFORUM = "tickets"
_TICKET_SELECT = (
    'SELECT id, title, "column", assignee, thread_id, created_by, created_at, updated_at, done_at '
    "FROM tickets"
)


class NoSuchTicket(LookupError):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(ticket_id)
        self.ticket_id = ticket_id


class Conflict(Exception):
    def __init__(self, assignee: str) -> None:
        super().__init__(f"ticket is already claimed by {assignee}")
        self.assignee = assignee


@dataclass(frozen=True)
class Ticket:
    id: int
    title: str
    column: str
    assignee: str | None
    thread_id: int
    created_by: str
    created_at: float
    updated_at: float
    done_at: float | None


@dataclass(frozen=True)
class TicketPage:
    tickets: tuple[Ticket, ...]
    total: int
    older: int | None
    newer: int | None


type _Edit = Callable[[Ticket], tuple[Ticket, str] | None]


class Tickets:
    def __init__(self, db: Database, forum: Forum) -> None:
        self._db = db
        self._forum = forum

    async def create(
        self,
        author: str,
        author_kind: AuthorKind,
        payload: Mapping[str, Any],
        *,
        signed: Verified | None = None,
    ) -> Ticket:
        column = _column(payload.get("column", COLUMNS[0]))
        # Forum.post commits its own transaction, so the thread is stored before the ticket row, not with it.
        opened = await self._forum.post(
            author,
            author_kind,
            SubforumTarget(TICKETS_SUBFORUM),
            payload,
            signed=signed,
            thread_kind="ticket",
        )
        now = time.time()

        def store(conn: sqlite3.Connection) -> Ticket:
            cursor = conn.execute(
                'INSERT INTO tickets (title, "column", thread_id, created_by, created_at, updated_at, done_at) '
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (payload["title"], column, opened.thread_id, author, now, now, _done_at(column, now)),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("INSERT reported no row id")
            return _read_ticket(conn, cursor.lastrowid)

        return await self._db.transact(store)

    async def claim(self, principal: str, ticket_id: int) -> Ticket:
        def assign(ticket: Ticket) -> tuple[Ticket, str]:
            if ticket.assignee is not None:
                raise Conflict(ticket.assignee)
            return replace(ticket, assignee=principal), f"{mentions.format(principal)} claimed this ticket"

        return await self._change(principal, ticket_id, assign)

    async def release(self, principal: str, ticket_id: int) -> Ticket:
        def unassign(ticket: Ticket) -> tuple[Ticket, str] | None:
            if ticket.assignee is None:
                return None
            note = f"{principal} released this ticket from {mentions.format(ticket.assignee)}"
            return replace(ticket, assignee=None), note

        return await self._change(principal, ticket_id, unassign)

    async def move(self, principal: str, ticket_id: int, column: str) -> Ticket:
        column = _column(column)

        def to_column(ticket: Ticket) -> tuple[Ticket, str] | None:
            if ticket.column == column:
                return None
            note = f"{principal} moved this ticket from {ticket.column} to {column}"
            if ticket.assignee is not None:
                note += f" (assigned to {mentions.format(ticket.assignee)})"
            return replace(ticket, column=column, done_at=_done_at(column, time.time())), note

        return await self._change(principal, ticket_id, to_column)

    async def find(self, column: str | None = None, assignee: str | None = None) -> list[Ticket]:
        def read(conn: sqlite3.Connection) -> list[Ticket]:
            rows = conn.execute(
                f'{_TICKET_SELECT} WHERE (?1 IS NULL OR "column" = ?1) AND (?2 IS NULL OR assignee = ?2) '
                "ORDER BY id",
                (column, assignee),
            )
            return [_ticket(row) for row in rows]

        return await self._db.read(read)

    async def page(
        self,
        *,
        column: str | None = None,
        assignee: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = 50,
    ) -> TicketPage:
        if column is not None:
            _column(column)
        if not 1 <= limit <= 200:
            raise Refused("bad_payload", "limit")
        if before is not None and after is not None:
            raise Refused("bad_payload", "cursor")
        if any(cursor is not None and not 0 <= cursor <= 2**63 - 1 for cursor in (before, after)):
            raise Refused("bad_payload", "cursor")
        return await self._db.read(lambda conn: _page(conn, column, assignee, before, after, limit))

    async def previews(self) -> dict[str, TicketPage]:
        return await self._db.read(
            lambda conn: {column: _page(conn, column, None, None, None, 20) for column in COLUMNS}
        )

    async def for_thread(self, thread_id: int) -> Ticket | None:
        def read(conn: sqlite3.Connection) -> Ticket | None:
            row = conn.execute(f"{_TICKET_SELECT} WHERE thread_id = ?", (thread_id,)).fetchone()
            return None if row is None else _ticket(row)

        return await self._db.read(read)

    async def get(self, ticket_id: int) -> Ticket:
        return await self._db.read(lambda conn: _read_ticket(conn, ticket_id))

    async def _change(self, principal: str, ticket_id: int, edit: _Edit) -> Ticket:
        def apply(conn: sqlite3.Connection) -> tuple[Ticket, str | None]:
            ticket = _read_ticket(conn, ticket_id)
            change = edit(ticket)
            if change is None:
                return ticket, None
            edited, note = change
            edited = replace(edited, updated_at=time.time())
            conn.execute(
                'UPDATE tickets SET "column" = ?, assignee = ?, updated_at = ?, done_at = ? WHERE id = ?',
                (edited.column, edited.assignee, edited.updated_at, edited.done_at, ticket_id),
            )
            return edited, note

        ticket, note = await self._db.transact(apply)
        # Forum.post commits its own transaction, so the note follows the change rather than joining it.
        # The actor authors it because the forum's wake rule skips a post's author: no self-wakes.
        if note is not None:
            await self._forum.post(principal, "system", ThreadTarget(ticket.thread_id), {"body": note})
        return ticket


def _page(
    conn: sqlite3.Connection,
    column: str | None,
    assignee: str | None,
    before: int | None,
    after: int | None,
    limit: int,
) -> TicketPage:
    filters = ["1 = 1"]
    values: list[str | int] = []
    if column is not None:
        filters.append('"column" = ?')
        values.append(column)
    if assignee is not None:
        filters.append("assignee = ?")
        values.append(assignee)
    where = " AND ".join(filters)
    bounds = " AND id < ?" if before is not None else " AND id > ?" if after is not None else ""
    cursor = before if before is not None else after
    params = [*values, *([] if cursor is None else [cursor]), limit]
    direction = "ASC" if after is not None else "DESC"
    rows = conn.execute(
        f"{_TICKET_SELECT} WHERE {where}{bounds} ORDER BY id {direction} LIMIT ?", params
    ).fetchall()
    if after is not None:
        rows.reverse()
    tickets = tuple(_ticket(row) for row in rows)
    total = conn.execute(f"SELECT COUNT(*) FROM tickets WHERE {where}", values).fetchone()[0]
    older = newer = None
    if tickets:
        if conn.execute(
            f"SELECT 1 FROM tickets WHERE {where} AND id < ? LIMIT 1", (*values, tickets[-1].id)
        ).fetchone():
            older = tickets[-1].id
        if conn.execute(
            f"SELECT 1 FROM tickets WHERE {where} AND id > ? LIMIT 1", (*values, tickets[0].id)
        ).fetchone():
            newer = tickets[0].id
    return TicketPage(tickets, total, older, newer)


def _column(value: object) -> str:
    if not isinstance(value, str) or value not in COLUMNS:
        raise Refused("bad_payload", "column")
    return value


def _done_at(column: str, now: float) -> float | None:
    return now if column == "done" else None


def _read_ticket(conn: sqlite3.Connection, ticket_id: int) -> Ticket:
    row = conn.execute(f"{_TICKET_SELECT} WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise NoSuchTicket(ticket_id)
    return _ticket(row)


def _ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(
        row["id"],
        row["title"],
        row["column"],
        row["assignee"],
        row["thread_id"],
        row["created_by"],
        row["created_at"],
        row["updated_at"],
        row["done_at"],
    )
