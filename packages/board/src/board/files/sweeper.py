import sqlite3
from functools import partial

from starlette.concurrency import run_in_threadpool

from board.db import Database
from board.files import LIVE_CONDITION
from board.files.storage import Store

SWEEP_SECONDS = 900


async def sweep(db: Database, store: Store, now: float) -> None:
    expired = await db.read(partial(_expired_ids, now=now))
    # Blobs go before rows: a crash in between leaves expired rows the next sweep retries, never
    # orphaned blobs that no row points at.
    await run_in_threadpool(_delete_blobs, store, expired)
    await db.transact(partial(_delete_rows, file_ids=expired))


def _delete_blobs(store: Store, file_ids: list[str]) -> None:
    for file_id in file_ids:
        store.delete(file_id)


def _expired_ids(conn: sqlite3.Connection, now: float) -> list[str]:
    return [row["id"] for row in conn.execute(f"SELECT id FROM files WHERE NOT ({LIVE_CONDITION})", (now,))]


def _delete_rows(conn: sqlite3.Connection, file_ids: list[str]) -> None:
    conn.executemany("DELETE FROM files WHERE id = ?", [(file_id,) for file_id in file_ids])
