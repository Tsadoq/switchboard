import re
import sqlite3
import time
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, BinaryIO, Self

from starlette.concurrency import run_in_threadpool

from board.db import Database
from board.files.storage import Store
from board.forum.actions import Refused
from board.identity.verifier import Verified

MAX_TTL_DAYS = 30
LIVE_CONDITION = "expires_at > ?"
_DAY_SECONDS = 86400
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*"
_MEDIA_TYPE = re.compile(f"{_TOKEN}/{_TOKEN}")
_ENTRY_COLUMNS = "id, name, content_type, size, sha256, uploaded_by, created_at, expires_at"


class NoSuchFile(LookupError):
    pass


@dataclass(frozen=True)
class Declaration:
    name: str
    content_type: str
    size: int
    sha256: str
    ttl_days: int | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise Refused("bad_payload", "name")
        content_type = payload.get("content_type")
        if not isinstance(content_type, str) or _MEDIA_TYPE.fullmatch(content_type) is None:
            raise Refused("bad_payload", "content_type")
        size = payload.get("size")
        if type(size) is not int or size < 0:
            raise Refused("bad_payload", "size")
        sha256 = payload.get("sha256")
        if not isinstance(sha256, str) or _SHA256_HEX.fullmatch(sha256) is None:
            raise Refused("bad_payload", "sha256")
        ttl_days = payload.get("ttl_days")
        if ttl_days is not None and (type(ttl_days) is not int or ttl_days < 1):
            raise Refused("bad_payload", "ttl_days")
        return cls(name, content_type, size, sha256, ttl_days)


@dataclass(frozen=True)
class FileEntry:
    id: str
    name: str
    content_type: str
    size: int
    sha256: str
    uploaded_by: str
    created_at: float
    expires_at: float


class Files:
    def __init__(self, db: Database, store: Store, default_ttl_days: int) -> None:
        self._db = db
        self._store = store
        self._default_ttl_days = default_ttl_days

    async def put(
        self,
        chunks: AsyncIterable[bytes],
        declared: Declaration,
        uploaded_by: str,
        *,
        signed: Verified | None = None,
    ) -> FileEntry:
        blob = await self._store.put(chunks, declared.sha256, declared.size)
        requested_days = self._default_ttl_days if declared.ttl_days is None else declared.ttl_days
        now = time.time()
        entry = FileEntry(
            blob.id,
            declared.name,
            declared.content_type,
            blob.size,
            blob.sha256,
            uploaded_by,
            now,
            now + min(requested_days, MAX_TTL_DAYS) * _DAY_SECONDS,
        )
        try:
            await self._db.transact(partial(_insert, entry=entry, signed=signed))
        except Exception:
            await run_in_threadpool(self._store.delete, blob.id)
            raise
        return entry

    async def live(self) -> list[FileEntry]:
        def read(conn: sqlite3.Connection) -> list[FileEntry]:
            rows = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM files WHERE {LIVE_CONDITION} ORDER BY created_at DESC, id",
                (time.time(),),
            )
            return [_entry(row) for row in rows]

        return await self._db.read(read)

    async def live_by_ids(self, ids: tuple[str, ...]) -> list[FileEntry]:
        def read(conn: sqlite3.Connection) -> list[FileEntry]:
            entries: list[FileEntry] = []
            now = time.time()
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT {_ENTRY_COLUMNS} FROM files WHERE id IN ({placeholders}) AND {LIVE_CONDITION}",
                    (*batch, now),
                )
                entries.extend(_entry(row) for row in rows)
            return entries

        return await self._db.read(read)

    async def open(self, file_id: str) -> tuple[FileEntry, BinaryIO]:
        def read(conn: sqlite3.Connection) -> FileEntry | None:
            row = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM files WHERE id = ? AND {LIVE_CONDITION}",
                (file_id, time.time()),
            ).fetchone()
            return None if row is None else _entry(row)

        entry = await self._db.read(read)
        if entry is None:
            raise NoSuchFile(file_id)
        try:
            return entry, self._store.open(entry.id)
        except FileNotFoundError:
            raise NoSuchFile(file_id) from None


def _insert(conn: sqlite3.Connection, entry: FileEntry, signed: Verified | None) -> None:
    conn.execute(
        "INSERT INTO files (id, name, content_type, size, sha256, uploaded_by, envelope_bytes, signature, "
        "key_fingerprint, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry.id,
            entry.name,
            entry.content_type,
            entry.size,
            entry.sha256,
            entry.uploaded_by,
            None if signed is None else signed.envelope_bytes,
            None if signed is None else signed.signature,
            None if signed is None else signed.key_fingerprint,
            entry.created_at,
            entry.expires_at,
        ),
    )


def _entry(row: sqlite3.Row) -> FileEntry:
    return FileEntry(
        row["id"],
        row["name"],
        row["content_type"],
        row["size"],
        row["sha256"],
        row["uploaded_by"],
        row["created_at"],
        row["expires_at"],
    )
