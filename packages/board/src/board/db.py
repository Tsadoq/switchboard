import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from starlette.concurrency import run_in_threadpool

SCHEMA_DIR = Path(__file__).with_name("schema")


def connect(path: Path) -> sqlite3.Connection:
    # autocommit=True leaves every BEGIN and COMMIT to Database.transact; it is also what stops
    # executescript in migrate from committing the open transaction before running its script.
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migrate(conn: sqlite3.Connection, schema_dir: Path = SCHEMA_DIR) -> None:
    applied = _applied_migrations(conn)
    for script in sorted(schema_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if script.name in applied:
            continue
        conn.executescript(script.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
            (script.name, time.time()),
        )


def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    ledger = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if ledger is None:
        return set()
    return {row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")}


@dataclass(frozen=True)
class Database:
    path: Path

    async def transact[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await run_in_threadpool(self._run_in_transaction, fn)

    async def read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await run_in_threadpool(self._run_read_only, fn)

    def _run_read_only[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with closing(connect(self.path)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN DEFERRED")
            try:
                return fn(conn)
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")

    def _run_in_transaction[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with closing(connect(self.path)) as conn:
            # IMMEDIATE takes the write lock up front: a deferred transaction whose WAL snapshot went
            # stale before its first write fails with SQLITE_BUSY_SNAPSHOT, which busy_timeout never retries.
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result
