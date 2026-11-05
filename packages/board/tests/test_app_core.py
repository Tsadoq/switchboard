import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest
from board import passwords, periodic
from board.db import Database, connect, migrate
from board.settings import Settings
from fastapi.testclient import TestClient


def test_healthz_reports_ok(client: TestClient) -> None:
    r = client.get("/healthz")

    assert r.status_code == 200, f"healthz failed: {r.status_code} {r.text}"
    body = r.json()
    assert body["status"] == "ok", f"status not ok: {body}"


@pytest.mark.usefixtures("client")
def test_migration_creates_tables(settings: Settings) -> None:
    with closing(sqlite3.connect(settings.db_path)) as conn:
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    missing = {"posts", "wakes", "tickets", "files", "post_files"} - tables
    assert not missing, f"migration did not create {sorted(missing)}; found {sorted(tables)}"


def test_migrate_on_migrated_database_keeps_existing_rows(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "board.sqlite3")) as conn:
        migrate(conn)
        conn.execute("INSERT INTO subforums (slug, title, description) VALUES ('general', 'General', '')")

        migrate(conn)

        slugs = [row["slug"] for row in conn.execute("SELECT slug FROM subforums")]
    assert slugs == ["general"], f"second migrate lost or duplicated rows: {slugs}"


@pytest.mark.anyio
async def test_transact_runs_off_event_loop(tmp_path: Path) -> None:
    database = Database(tmp_path / "board.sqlite3")
    loop_thread = threading.get_ident()

    transact_thread = await database.transact(lambda conn: threading.get_ident())

    assert transact_thread != loop_thread, "transact ran its callable on the event loop thread"


@pytest.mark.anyio
async def test_transact_rolls_back_when_callable_raises(tmp_path: Path) -> None:
    database = Database(tmp_path / "board.sqlite3")
    await database.transact(lambda conn: conn.execute("CREATE TABLE notes (body TEXT)"))

    def insert_then_fail(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO notes (body) VALUES ('half-written')")
        raise RuntimeError("abort after the insert")

    with pytest.raises(RuntimeError):
        await database.transact(insert_then_fail)

    rows = await database.transact(lambda conn: conn.execute("SELECT body FROM notes").fetchall())
    assert rows == [], f"insert from the failed transaction persisted: {[tuple(row) for row in rows]}"


def test_hash_password_round_trip_and_rejects_wrong() -> None:
    stored = passwords.hash_password("correct horse")

    assert "correct horse" not in stored, f"stored hash carries the plaintext: {stored!r}"
    assert passwords.verify_password("correct horse", stored), f"right password rejected by {stored!r}"
    assert not passwords.verify_password("battery staple", stored), f"wrong password accepted by {stored!r}"


@pytest.mark.anyio
async def test_periodic_every_keeps_running_after_an_exception() -> None:
    calls = 0
    called_again = asyncio.Event()

    async def fails_first_time() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first run fails")
        called_again.set()

    async with periodic.every(0.01, fails_first_time):
        try:
            await asyncio.wait_for(called_again.wait(), timeout=5)
        except TimeoutError:
            pytest.fail(f"periodic task stopped after its first exception; calls={calls}")


@pytest.mark.anyio
async def test_read_does_not_wait_for_a_held_write_lock(tmp_path: Path) -> None:
    database = Database(tmp_path / "board.sqlite3")
    await database.transact(lambda conn: conn.execute("CREATE TABLE notes (body TEXT)"))
    with closing(connect(database.path)) as writer:
        writer.execute("PRAGMA busy_timeout=0")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO notes (body) VALUES ('uncommitted')")

        rows = await asyncio.wait_for(
            database.read(lambda conn: conn.execute("SELECT body FROM notes").fetchall()), timeout=2
        )

        writer.execute("ROLLBACK")
    assert rows == [], f"read saw another connection's uncommitted row: {[tuple(row) for row in rows]}"


@pytest.mark.anyio
async def test_read_refuses_writes(tmp_path: Path) -> None:
    database = Database(tmp_path / "board.sqlite3")
    await database.transact(lambda conn: conn.execute("CREATE TABLE notes (body TEXT)"))

    with pytest.raises(sqlite3.OperationalError):
        await database.read(lambda conn: conn.execute("INSERT INTO notes (body) VALUES ('sneaky')"))

    rows = await database.transact(lambda conn: conn.execute("SELECT body FROM notes").fetchall())
    assert rows == [], "a write inside read persisted"


@pytest.mark.parametrize(
    ("query", "index"),
    [
        ("DELETE FROM nonces WHERE seen_at < 1", "nonces_seen_at"),
        ("SELECT author, COUNT(*) FROM posts WHERE created_at >= 1 GROUP BY author", "posts_created"),
        (
            "SELECT acked_at - created_at FROM wakes WHERE acked_at IS NOT NULL AND created_at >= 1",
            "wakes_created",
        ),
    ],
)
def test_windowed_queries_use_an_index(tmp_path: Path, query: str, index: str) -> None:
    with closing(connect(tmp_path / "board.sqlite3")) as conn:
        migrate(conn)
        plan = " ".join(row["detail"] for row in conn.execute(f"EXPLAIN QUERY PLAN {query}"))

    assert index in plan, f"query plan does not use {index}: {plan}"


def test_public_url_defaults_to_the_local_listener() -> None:
    settings = Settings.from_env({})

    assert settings.public_url == "http://127.0.0.1:8080", f"public url default: {settings.public_url}"
