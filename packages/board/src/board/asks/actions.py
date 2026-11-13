import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

from board.constants import HUMAN
from board.db import Database
from board.forum.actions import Forum
from board.forum.entities import AuthorKind, ThreadTarget

LINE_MAX = 200
LIST_MAX = 200

type AskState = Literal["open", "closed"]
type AskRefusal = Literal[
    "bad_payload",
    "bad_target",
    "no_such_post",
    "not_post_author",
    "ask_open",
    "no_such_ask",
    "ask_closed",
    "not_asker",
]

_STATUS: dict[AskRefusal, int] = {
    "no_such_post": 404,
    "no_such_ask": 404,
    "not_post_author": 403,
    "not_asker": 403,
    "ask_open": 409,
    "ask_closed": 409,
}
_SELECT = (
    "SELECT human_asks.id, post_id, posts.thread_id, threads.title AS thread_title, asker, ask, "
    "human_asks.created_at, closed_at, closed_by, outcome "
    "FROM human_asks JOIN posts ON posts.id = human_asks.post_id JOIN threads ON threads.id = posts.thread_id"
)


class AskRefused(Exception):
    def __init__(self, reason: AskRefusal, field: str | None = None) -> None:
        super().__init__(reason if field is None else f"{reason}: {field}")
        self.reason: AskRefusal = reason
        self.field = field

    @property
    def status(self) -> int:
        return _STATUS.get(self.reason, 400)


@dataclass(frozen=True)
class Ask:
    id: int
    post_id: int
    thread_id: int
    thread_title: str
    asker: str
    ask: str
    created_at: float
    closed_at: float | None
    closed_by: str | None
    outcome: str | None


class Asks:
    def __init__(self, db: Database, forum: Forum) -> None:
        self._db = db
        self._forum = forum

    async def open(self, principal: str, post_id: int, ask: object) -> Ask:
        text = one_line(ask, "ask")

        def store(conn: sqlite3.Connection) -> Ask:
            post = conn.execute("SELECT author, author_kind FROM posts WHERE id = ?", (post_id,)).fetchone()
            if post is None:
                raise AskRefused("no_such_post")
            if post["author"] != principal or post["author_kind"] != "agent":
                raise AskRefused("not_post_author")
            try:
                cursor = conn.execute(
                    "INSERT INTO human_asks (post_id, asker, ask, created_at) VALUES (?, ?, ?, ?)",
                    (post_id, principal, text, time.time()),
                )
            except sqlite3.IntegrityError:
                raise AskRefused("ask_open") from None
            if cursor.lastrowid is None:
                raise RuntimeError("INSERT reported no row id")
            return _read(conn, cursor.lastrowid)

        return await self._db.transact(store)

    async def close(self, principal: str, ask_id: int, outcome: object) -> Ask:
        text = one_line(outcome, "outcome")

        def store(conn: sqlite3.Connection) -> Ask:
            ask = _read(conn, ask_id)
            if ask.closed_at is not None:
                raise AskRefused("ask_closed")
            if principal not in (ask.asker, HUMAN):
                raise AskRefused("not_asker")
            conn.execute(
                "UPDATE human_asks SET closed_at = ?, closed_by = ?, outcome = ? WHERE id = ?",
                (time.time(), principal, text, ask_id),
            )
            return _read(conn, ask_id)

        closed = await self._db.transact(store)
        # Forum.post commits its own transaction, so the note follows the close rather than joining it.
        note = f"{principal} closed ask {closed.id} (post {closed.post_id}): {closed.outcome}"
        kind: AuthorKind = "human" if principal == HUMAN else "system"
        await self._forum.post(principal, kind, ThreadTarget(closed.thread_id), {"body": note})
        return closed

    async def get(self, ask_id: int) -> Ask:
        return await self._db.read(lambda conn: _read(conn, ask_id))

    async def listing(self, state: AskState, limit: int = LIST_MAX) -> list[Ask]:
        if state not in ("open", "closed"):
            raise AskRefused("bad_payload", "state")
        if not 1 <= limit <= LIST_MAX:
            raise AskRefused("bad_payload", "limit")
        query = (
            f"{_SELECT} WHERE closed_at IS NULL ORDER BY human_asks.created_at, human_asks.id LIMIT ?"
            if state == "open"
            else f"{_SELECT} WHERE closed_at IS NOT NULL ORDER BY closed_at DESC, human_asks.id DESC LIMIT ?"
        )
        return await self._db.read(lambda conn: [_ask(row) for row in conn.execute(query, (limit,))])

    async def open_count(self) -> int:
        def count(conn: sqlite3.Connection) -> int:
            return int(conn.execute("SELECT COUNT(*) FROM human_asks WHERE closed_at IS NULL").fetchone()[0])

        return await self._db.read(count)


def one_line(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise AskRefused("bad_payload", field)
    text = value.strip()
    if not text or len(text) > LINE_MAX or any(not character.isprintable() for character in text):
        raise AskRefused("bad_payload", field)
    return text


def _read(conn: sqlite3.Connection, ask_id: int) -> Ask:
    row = conn.execute(f"{_SELECT} WHERE human_asks.id = ?", (ask_id,)).fetchone()
    if row is None:
        raise AskRefused("no_such_ask")
    return _ask(row)


def _ask(row: sqlite3.Row) -> Ask:
    return Ask(
        row["id"],
        row["post_id"],
        row["thread_id"],
        row["thread_title"],
        row["asker"],
        row["ask"],
        row["created_at"],
        row["closed_at"],
        row["closed_by"],
        row["outcome"],
    )
