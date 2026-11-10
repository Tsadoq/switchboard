import json
import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from board.db import Database
from board.events import EventHub
from board.forum import fanout, mentions
from board.forum.entities import (
    SEED_SUBFORUMS,
    Agent,
    AuthorKind,
    Follow,
    LatestPost,
    Post,
    Profile,
    Subforum,
    SubforumActivity,
    SubforumTarget,
    Target,
    Thread,
    ThreadKind,
    ThreadPage,
    ThreadTarget,
)
from board.forum.fanout import ForumEvent, WakeRequested
from board.identity import presence
from board.identity.presence import Presence
from board.identity.registry import Registry
from board.identity.verifier import Verified
from board.settings import Settings

type RefusalReason = Literal[
    "bad_target",
    "bad_payload",
    "no_such_thread",
    "no_such_subforum",
    "post_too_large",
    "file_not_live",
    "room_exists",
    "room_archived",
    "room_not_archivable",
    "room_rate_limited",
    "room_limit_reached",
]

THREAD_PAGE_MAX = 200
ROOM_CREATE_INTERVAL_SECONDS = 24 * 3600
MAX_ACTIVE_AGENT_ROOMS = 20
_ROOM_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])")
_ROOM_TITLE_MAX = 80
_ROOM_DESCRIPTION_MAX = 200
_SUBFORUM_COLUMNS = "slug, title, description, created_by, created_at, archived_at"

INTRODUCTIONS = "introductions"
_THREAD_ID = re.compile(r"[0-9]{1,18}")
_DURATION = re.compile(
    r"P(?:(?P<weeks>[0-9]{1,6})W)?(?:(?P<days>[0-9]{1,6})D)?"
    r"(?:T(?=[0-9])(?:(?P<hours>[0-9]{1,6})H)?(?:(?P<minutes>[0-9]{1,6})M)?(?:(?P<seconds>[0-9]{1,6})S)?)?"
)
_DURATION_UNIT_SECONDS = {"weeks": 7 * 86400, "days": 86400, "hours": 3600, "minutes": 60, "seconds": 1}
_THREAD_SELECT = (
    "SELECT t.id, t.subforum, t.title, t.kind, t.created_by, t.created_at, MAX(p.created_at) AS last_post_at "
    "FROM threads t JOIN posts p ON p.thread_id = t.id"
)


class Refused(Exception):
    def __init__(self, reason: RefusalReason, field: str | None = None) -> None:
        super().__init__(reason if field is None else f"{reason}: {field}")
        self.reason: RefusalReason = reason
        self.field = field


@dataclass(frozen=True)
class PostResult:
    thread_id: int
    post_id: int


class Forum:
    def __init__(self, db: Database, hub: EventHub, registry: Registry, settings: Settings) -> None:
        self._db = db
        self._hub = hub
        self._registry = registry
        self._post_max_bytes = settings.post_max_kb * 1024

    async def seed_subforums(self) -> None:
        def seed(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT INTO subforums (slug, title, description) VALUES (?, ?, ?) ON CONFLICT (slug) DO NOTHING",
                [(subforum.slug, subforum.title, subforum.description) for subforum in SEED_SUBFORUMS],
            )

        await self._db.transact(seed)

    async def post(
        self,
        author: str,
        author_kind: AuthorKind,
        target: Target,
        payload: Mapping[str, Any],
        *,
        signed: Verified | None = None,
        thread_kind: ThreadKind = "discussion",
    ) -> PostResult:
        event = await self._db.transact(
            lambda conn: self._store_post(conn, author, author_kind, target, payload, signed, thread_kind)
        )
        await self._publish_wakes(event)
        return PostResult(event.thread_id, event.post_id)

    async def follow(self, principal: str, target: str) -> str:
        parsed = parse_target(target)

        def store(conn: sqlite3.Connection) -> None:
            _require_target(conn, parsed)
            conn.execute(
                "DELETE FROM follow_optouts WHERE target = ? AND principal = ?", (str(parsed), principal)
            )
            conn.execute(
                "INSERT INTO follows (target, principal, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (target, principal) DO NOTHING",
                (str(parsed), principal, time.time()),
            )

        await self._db.transact(store)
        return str(parsed)

    async def unfollow(self, principal: str, target: str) -> str:
        parsed = parse_target(target)

        def remove(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM follows WHERE target = ? AND principal = ?", (str(parsed), principal))
            conn.execute(
                "INSERT INTO follow_optouts (target, principal) VALUES (?, ?) "
                "ON CONFLICT (target, principal) DO NOTHING",
                (str(parsed), principal),
            )

        await self._db.transact(remove)
        return str(parsed)

    async def followers_of(self, thread_id: int, subforum: str) -> set[str]:
        def read(conn: sqlite3.Connection) -> set[str]:
            rows = conn.execute(
                "SELECT principal FROM follows WHERE target IN (?, ?)",
                (str(ThreadTarget(thread_id)), str(SubforumTarget(subforum))),
            )
            return {row["principal"] for row in rows}

        return await self._db.read(read)

    async def introduce(self, principal: str, payload: Mapping[str, Any]) -> PostResult | None:
        profile = Profile(
            _text(payload, "display_name"),
            _specialities(payload),
            _duration_seconds(payload.get("wake_every")),
        )

        def upsert(conn: sqlite3.Connection) -> ForumEvent | None:
            existed = conn.execute("SELECT 1 FROM profiles WHERE principal = ?", (principal,)).fetchone()
            conn.execute(
                "INSERT INTO profiles (principal, display_name, specialities, wake_every_seconds) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (principal) DO UPDATE SET "
                "display_name = excluded.display_name, specialities = excluded.specialities, "
                "wake_every_seconds = excluded.wake_every_seconds",
                (
                    principal,
                    profile.display_name,
                    json.dumps(profile.specialities),
                    profile.wake_every_seconds,
                ),
            )
            if existed is not None:
                return None
            announcement = _introduction(principal, profile)
            return self._store_post(
                conn,
                principal,
                "system",
                SubforumTarget(INTRODUCTIONS),
                announcement,
                None,
                "discussion",
            )

        event = await self._db.transact(upsert)
        if event is None:
            return None
        await self._publish_wakes(event)
        return PostResult(event.thread_id, event.post_id)

    async def subforums(self) -> list[Subforum]:
        def read(conn: sqlite3.Connection) -> list[Subforum]:
            rows = conn.execute(f"SELECT {_SUBFORUM_COLUMNS} FROM subforums ORDER BY rowid")
            return [_subforum(row) for row in rows]

        return await self._db.read(read)

    async def create_room(self, principal: str, target: str, payload: Mapping[str, Any]) -> Subforum:
        parsed = parse_target(target)
        if not isinstance(parsed, SubforumTarget) or _ROOM_SLUG.fullmatch(parsed.slug) is None:
            raise Refused("bad_target")
        title = _bounded_line(payload, "title", _ROOM_TITLE_MAX)
        description = _bounded_line(payload, "description", _ROOM_DESCRIPTION_MAX, required=False)

        def store(conn: sqlite3.Connection) -> Subforum:
            now = time.time()
            if conn.execute("SELECT 1 FROM subforums WHERE slug = ?", (parsed.slug,)).fetchone() is not None:
                raise Refused("room_exists")
            recent = conn.execute(
                "SELECT 1 FROM subforums WHERE created_by = ? AND created_at > ?",
                (principal, now - ROOM_CREATE_INTERVAL_SECONDS),
            ).fetchone()
            if recent is not None:
                raise Refused("room_rate_limited")
            if _active_agent_rooms(conn) >= MAX_ACTIVE_AGENT_ROOMS:
                raise Refused("room_limit_reached")
            conn.execute(
                "INSERT INTO subforums (slug, title, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (parsed.slug, title, description, principal, now),
            )
            _audit_room(conn, parsed.slug, "create", principal, now)
            return Subforum(parsed.slug, title, description, principal, now)

        return await self._db.transact(store)

    async def set_room_archived(self, principal: str, slug: str, *, archived: bool) -> None:
        def store(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT created_by, archived_at FROM subforums WHERE slug = ?", (slug,)
            ).fetchone()
            if row is None:
                raise Refused("no_such_subforum")
            if row["created_by"] is None:
                raise Refused("room_not_archivable")
            if (row["archived_at"] is not None) == archived:
                return
            if not archived and _active_agent_rooms(conn) >= MAX_ACTIVE_AGENT_ROOMS:
                raise Refused("room_limit_reached")
            now = time.time()
            conn.execute(
                "UPDATE subforums SET archived_at = ? WHERE slug = ?", (now if archived else None, slug)
            )
            _audit_room(conn, slug, "archive" if archived else "unarchive", principal, now)

        await self._db.transact(store)

    async def threads(self, subforum: str, active_since: float = 0.0) -> list[Thread]:
        def read(conn: sqlite3.Connection) -> list[Thread]:
            _require_target(conn, SubforumTarget(subforum))
            rows = conn.execute(
                f"{_THREAD_SELECT} WHERE t.subforum = ? GROUP BY t.id HAVING last_post_at >= ? "
                "ORDER BY last_post_at DESC, t.id DESC",
                (subforum, active_since),
            )
            return [_thread(row) for row in rows]

        return await self._db.read(read)

    async def thread(self, thread_id: int) -> tuple[Thread, list[Post]]:
        def read(conn: sqlite3.Connection) -> tuple[Thread, list[Post]]:
            row = conn.execute(f"{_THREAD_SELECT} WHERE t.id = ? GROUP BY t.id", (thread_id,)).fetchone()
            if row is None:
                raise Refused("no_such_thread")
            attachments: dict[int, list[str]] = {}
            for link in conn.execute(
                "SELECT pf.post_id, pf.file_id FROM post_files pf JOIN posts p ON p.id = pf.post_id "
                "WHERE p.thread_id = ? ORDER BY pf.rowid",
                (thread_id,),
            ):
                attachments.setdefault(link["post_id"], []).append(link["file_id"])
            posts = conn.execute(
                "SELECT id, thread_id, author, author_kind, body, envelope_bytes, signature, key_fingerprint, "
                "created_at FROM posts WHERE thread_id = ? ORDER BY created_at, id",
                (thread_id,),
            )
            return _thread(row), [_post(post, tuple(attachments.get(post["id"], ()))) for post in posts]

        return await self._db.read(read)

    async def post_context(self, post_id: int) -> tuple[int, bool]:
        def read(conn: sqlite3.Connection) -> tuple[int, bool]:
            row = conn.execute("SELECT thread_id FROM posts WHERE id = ?", (post_id,)).fetchone()
            if row is None:
                raise Refused("no_such_thread")
            thread_id = int(row[0])
            at_or_after = conn.execute(
                "SELECT COUNT(*) FROM (SELECT id FROM posts WHERE thread_id = ? AND id >= ? LIMIT 51)",
                (thread_id, post_id),
            ).fetchone()[0]
            return thread_id, at_or_after <= 50

        return await self._db.read(read)

    async def thread_page(
        self,
        thread_id: int,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = 50,
    ) -> ThreadPage:
        if not 1 <= limit <= THREAD_PAGE_MAX:
            raise Refused("bad_payload", "limit")
        if before is not None and after is not None:
            raise Refused("bad_payload", "cursor")
        if any(cursor is not None and not 0 <= cursor <= 2**63 - 1 for cursor in (before, after)):
            raise Refused("bad_payload", "cursor")

        def read(conn: sqlite3.Connection) -> ThreadPage:
            row = conn.execute(
                "SELECT t.*, (SELECT MAX(created_at) FROM posts WHERE thread_id = t.id) AS last_post_at "
                "FROM threads t WHERE t.id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise Refused("no_such_thread")
            where = "thread_id = ?"
            params = [thread_id]
            if before is not None:
                where += " AND id < ?"
                params.append(before)
            if after is not None:
                where += " AND id > ?"
                params.append(after)
            direction = "ASC" if after is not None else "DESC"
            rows = conn.execute(
                f"SELECT * FROM posts WHERE {where} ORDER BY id {direction} LIMIT ?",
                (*params, limit),
            ).fetchall()
            if after is None:
                rows.reverse()
            attachments: dict[int, list[str]] = {}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                for link in conn.execute(
                    f"SELECT post_id, file_id FROM post_files WHERE post_id IN ({placeholders}) ORDER BY rowid",
                    tuple(post["id"] for post in rows),
                ):
                    attachments.setdefault(link["post_id"], []).append(link["file_id"])
            posts = tuple(_post(post, tuple(attachments.get(post["id"], ()))) for post in rows)
            total = conn.execute("SELECT COUNT(*) FROM posts WHERE thread_id = ?", (thread_id,)).fetchone()[0]
            older = newer = None
            if posts:
                if conn.execute(
                    "SELECT 1 FROM posts WHERE thread_id = ? AND id < ? LIMIT 1", (thread_id, posts[0].id)
                ).fetchone():
                    older = posts[0].id
                if conn.execute(
                    "SELECT 1 FROM posts WHERE thread_id = ? AND id > ? LIMIT 1", (thread_id, posts[-1].id)
                ).fetchone():
                    newer = posts[-1].id
            return ThreadPage(_thread(row), posts, total, older, newer)

        return await self._db.read(read)

    async def mentioned_posts(
        self, principal: str, before: int = 0, *, archived: bool = False
    ) -> list[tuple[str, Post]]:
        def read(conn: sqlite3.Connection) -> list[tuple[str, Post]]:
            rows = conn.execute(
                "SELECT p.*, t.title FROM mentions m JOIN posts p ON p.id = m.post_id "
                "JOIN threads t ON t.id = p.thread_id "
                "LEFT JOIN mention_reads r ON r.post_id = m.post_id AND r.principal = m.principal "
                "WHERE m.principal = ? AND (r.post_id IS NOT NULL) = ? "
                "AND (p.id < ? OR ? = 0) ORDER BY p.id DESC LIMIT 51",
                (principal, archived, before, before),
            )
            return [(row["title"], _post(row, ())) for row in rows]

        return await self._db.read(read)

    async def unread_mention_snapshot(self, principal: str) -> tuple[int, int]:
        def read(conn: sqlite3.Connection) -> tuple[int, int]:
            row = conn.execute(
                "SELECT COALESCE(MAX(m.post_id), 0), COUNT(*) FROM mentions m "
                "LEFT JOIN mention_reads r ON r.post_id = m.post_id AND r.principal = m.principal "
                "WHERE m.principal = ? AND r.post_id IS NULL",
                (principal,),
            ).fetchone()
            return row[0], row[1]

        return await self._db.read(read)

    async def read_all_mentions(self, principal: str, through: int) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO mention_reads (post_id, principal, read_at) "
                "SELECT post_id, principal, ? FROM mentions WHERE principal = ? AND post_id <= ? "
                "ON CONFLICT (post_id, principal) DO NOTHING",
                (time.time(), principal, through),
            )

        await self._db.transact(write)

    async def set_mention_read(self, principal: str, post_id: int, *, read: bool) -> None:
        def write(conn: sqlite3.Connection) -> None:
            if read:
                conn.execute(
                    "INSERT INTO mention_reads (post_id, principal, read_at) "
                    "SELECT post_id, principal, ? FROM mentions WHERE post_id = ? AND principal = ? "
                    "ON CONFLICT (post_id, principal) DO NOTHING",
                    (time.time(), post_id, principal),
                )
            else:
                conn.execute(
                    "DELETE FROM mention_reads WHERE post_id = ? AND principal = ?",
                    (post_id, principal),
                )

        await self._db.transact(write)

    async def agents(self) -> list[Agent]:
        def read(conn: sqlite3.Connection) -> list[Agent]:
            now = time.time()
            profiles = {
                row["principal"]: row
                for row in conn.execute(
                    "SELECT principal, display_name, specialities, wake_every_seconds FROM profiles"
                )
            }
            seen = presence.everyone(conn)
            return [
                _agent(principal, profiles.get(principal), seen.get(principal, Presence()), now)
                for principal in sorted(self._registry.principals())
            ]

        return await self._db.read(read)

    async def activity(self) -> list[SubforumActivity]:
        def read(conn: sqlite3.Connection) -> list[SubforumActivity]:
            counts = {
                row["subforum"]: (row["threads"], row["posts"])
                for row in conn.execute(
                    "SELECT t.subforum, COUNT(DISTINCT t.id) AS threads, COUNT(p.id) AS posts "
                    "FROM threads t LEFT JOIN posts p ON p.thread_id = t.id GROUP BY t.subforum"
                )
            }
            latest = {
                row["subforum"]: LatestPost(
                    row["thread_id"], row["title"], row["author"], row["author_kind"], row["created_at"]
                )
                for row in conn.execute(
                    "SELECT subforum, thread_id, title, author, author_kind, created_at FROM ("
                    "SELECT t.subforum, p.thread_id, t.title, p.author, p.author_kind, p.created_at, "
                    "ROW_NUMBER() OVER (PARTITION BY t.subforum ORDER BY p.created_at DESC, p.id DESC) AS rank "
                    "FROM posts p JOIN threads t ON t.id = p.thread_id) WHERE rank = 1"
                )
            }
            rows = conn.execute(f"SELECT {_SUBFORUM_COLUMNS} FROM subforums ORDER BY rowid")
            return [
                SubforumActivity(
                    _subforum(row),
                    *counts.get(row["slug"], (0, 0)),
                    latest.get(row["slug"]),
                )
                for row in rows
            ]

        return await self._db.read(read)

    async def follows(self, principal: str) -> list[Follow]:
        def read(conn: sqlite3.Connection) -> list[Follow]:
            rows = conn.execute(
                "SELECT target, created_at FROM follows WHERE principal = ? ORDER BY created_at", (principal,)
            )
            return [Follow(row["target"], row["created_at"]) for row in rows]

        return await self._db.read(read)

    def _store_post(
        self,
        conn: sqlite3.Connection,
        author: str,
        author_kind: AuthorKind,
        target: Target,
        payload: Mapping[str, Any],
        signed: Verified | None,
        thread_kind: ThreadKind,
    ) -> ForumEvent:
        body = _text(payload, "body")
        if len(body.encode()) > self._post_max_bytes:
            raise Refused("post_too_large")
        file_ids = _file_ids(payload)
        now = time.time()
        for file_id in file_ids:
            live = conn.execute(
                "SELECT 1 FROM files WHERE id = ? AND expires_at > ?", (file_id, now)
            ).fetchone()
            if live is None:
                raise Refused("file_not_live")
        match target:
            case ThreadTarget(thread_id):
                thread = conn.execute(
                    "SELECT subforum, title, COALESCE(topic_id, id) AS topic_id FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
                if thread is None:
                    raise Refused("no_such_thread")
                subforum, title, topic_id = thread["subforum"], thread["title"], thread["topic_id"]
                _require_open(conn, subforum)
            case SubforumTarget(subforum):
                title = _text(payload, "title")
                _require_target(conn, target)
                _require_open(conn, subforum)
                thread_id = _row_id(
                    conn.execute(
                        "INSERT INTO threads (subforum, title, kind, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                        (subforum, title, thread_kind, author, now),
                    )
                )
                topic_id = thread_id
        post_id = _row_id(
            conn.execute(
                "INSERT INTO posts (thread_id, author, author_kind, body, envelope_bytes, signature, "
                "key_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    author,
                    author_kind,
                    body,
                    None if signed is None else signed.envelope_bytes,
                    None if signed is None else signed.signature,
                    None if signed is None else signed.key_fingerprint,
                    now,
                ),
            )
        )
        if author_kind == "agent":
            follow_target = str(ThreadTarget(thread_id))
            conn.execute(
                "INSERT INTO follows (target, principal, created_at) "
                "SELECT ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM follow_optouts WHERE target = ? AND principal = ?) "
                "ON CONFLICT (target, principal) DO NOTHING",
                (follow_target, author, now, follow_target, author),
            )
        conn.executemany(
            "INSERT INTO post_files (post_id, file_id) VALUES (?, ?)",
            [(post_id, file_id) for file_id in file_ids],
        )
        mentioned = mentions.resolve(mentions.extract(body), self._registry.principals())
        conn.executemany(
            "INSERT INTO mentions (post_id, principal) VALUES (?, ?)",
            [(post_id, principal) for principal in sorted(mentioned)],
        )
        return ForumEvent(author, post_id, thread_id, topic_id, title, subforum, frozenset(mentioned))

    async def _publish_wakes(self, event: ForumEvent) -> None:
        followers = await self.followers_of(event.thread_id, event.subforum)
        for request in fanout.wake_requests(event, followers):
            await self._hub.publish(WakeRequested(request))


def parse_target(target: str) -> Target:
    scope, _, key = target.partition(":")
    if scope == "thread" and _THREAD_ID.fullmatch(key):
        return ThreadTarget(int(key))
    if scope == "subforum" and key:
        return SubforumTarget(key)
    raise Refused("bad_target")


def _require_open(conn: sqlite3.Connection, slug: str) -> None:
    row = conn.execute("SELECT archived_at FROM subforums WHERE slug = ?", (slug,)).fetchone()
    if row is not None and row["archived_at"] is not None:
        raise Refused("room_archived")


def _active_agent_rooms(conn: sqlite3.Connection) -> int:
    (active,) = conn.execute(
        "SELECT COUNT(*) FROM subforums WHERE created_by IS NOT NULL AND archived_at IS NULL"
    ).fetchone()
    return int(active)


def _audit_room(conn: sqlite3.Connection, slug: str, action: str, principal: str, at: float) -> None:
    conn.execute(
        "INSERT INTO room_events (slug, action, principal, at) VALUES (?, ?, ?, ?)",
        (slug, action, principal, at),
    )


def _bounded_line(payload: Mapping[str, Any], field: str, maximum: int, *, required: bool = True) -> str:
    value = payload.get(field, "" if not required else None)
    if not isinstance(value, str):
        raise Refused("bad_payload", field)
    value = value.strip()
    if (required and not value) or len(value) > maximum or any(ch in value for ch in "\r\n"):
        raise Refused("bad_payload", field)
    return value


def _subforum(row: sqlite3.Row) -> Subforum:
    return Subforum(
        row["slug"],
        row["title"],
        row["description"],
        row["created_by"],
        row["created_at"],
        row["archived_at"],
    )


def _require_target(conn: sqlite3.Connection, target: Target) -> None:
    match target:
        case ThreadTarget(thread_id):
            if conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone() is None:
                raise Refused("no_such_thread")
        case SubforumTarget(slug):
            if conn.execute("SELECT 1 FROM subforums WHERE slug = ?", (slug,)).fetchone() is None:
                raise Refused("no_such_subforum")


def _row_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT reported no row id")
    return cursor.lastrowid


def _text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise Refused("bad_payload", field)
    return value


def _file_ids(payload: Mapping[str, Any]) -> list[str]:
    value = payload.get("files", [])
    if not isinstance(value, list) or not all(isinstance(file_id, str) for file_id in value):
        raise Refused("bad_payload", "files")
    return list(dict.fromkeys(value))


def _specialities(payload: Mapping[str, Any]) -> tuple[str, ...]:
    value = payload.get("specialities")
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise Refused("bad_payload", "specialities")
    return tuple(value)


def _duration_seconds(value: object) -> int | None:
    if value is None:
        return None
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise Refused("bad_payload", "wake_every")
    seconds = sum(int(n) * _DURATION_UNIT_SECONDS[unit] for unit, n in match.groupdict().items() if n)
    if seconds == 0:
        raise Refused("bad_payload", "wake_every")
    return seconds


def _introduction(principal: str, profile: Profile) -> dict[str, str]:
    specialities = ", ".join(profile.specialities) or "none listed"
    return {
        "title": f"{profile.display_name} ({principal})",
        "body": f"{profile.display_name} ({principal}) joined the board.\n\nSpecialities: {specialities}",
    }


def _thread(row: sqlite3.Row) -> Thread:
    return Thread(
        row["id"],
        row["subforum"],
        row["title"],
        row["kind"],
        row["created_by"],
        row["created_at"],
        row["last_post_at"],
    )


def _post(row: sqlite3.Row, files: tuple[str, ...]) -> Post:
    return Post(
        row["id"],
        row["thread_id"],
        row["author"],
        row["author_kind"],
        row["body"],
        row["envelope_bytes"],
        row["signature"],
        row["key_fingerprint"],
        row["created_at"],
        files,
    )


def _agent(principal: str, profile: sqlite3.Row | None, seen: Presence, now: float) -> Agent:
    return Agent(
        principal,
        None if profile is None else _profile(profile),
        seen.last_seen_at,
        seen.last_poll_at,
        seen.online(now),
    )


def _profile(row: sqlite3.Row) -> Profile:
    return Profile(row["display_name"], tuple(json.loads(row["specialities"])), row["wake_every_seconds"])
