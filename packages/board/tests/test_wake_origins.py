import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from board.db import Database, connect, migrate
from board.forum.fanout import WakeRequest
from board.wakes.bus import WakeBus
from board.wakes.origins import classify_origin


@pytest.fixture
def origin_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(tmp_path / "board.db")) as conn:
        migrate(conn)
        conn.execute("INSERT INTO subforums (slug, title, description) VALUES ('test', 'Test', '')")
        conn.execute(
            "INSERT INTO threads (id, subforum, title, kind, created_by, created_at) "
            "VALUES (1, 'test', 'Test', 'discussion', 'human', 0)"
        )
        for post_id, author, kind in (
            (1, "human", "human"),
            (2, "pi@dev", "agent"),
            (3, "human", "system"),
            (4, "pi@dev", "system"),
            (5, "human", "agent"),
            (6, "impostor", "human"),
        ):
            conn.execute(
                "INSERT INTO posts (id, thread_id, author, author_kind, body, created_at) "
                "VALUES (?, 1, ?, ?, 'incident stop @human', 0)",
                (post_id, author, kind),
            )
        conn.commit()
        yield conn


@pytest.mark.parametrize(
    ("source_ref", "expected"),
    [
        ("post:1", "human"),
        ("post:2", "budgeted"),
        ("post:3", "human"),
        ("post:4", "budgeted"),
        ("post:5", "budgeted"),
        ("post:6", "budgeted"),
        ("post:7", "budgeted"),
        ("post:01", "budgeted"),
        ("post:+1", "budgeted"),
        ("post:١", "budgeted"),
        ("post:1\n", "budgeted"),
        ("post:9223372036854775808", "budgeted"),
        ("post:" + "9" * 5000, "budgeted"),
        ("human", "budgeted"),
        ("", "budgeted"),
    ],
)
def test_canonical_server_post_origin(origin_db: sqlite3.Connection, source_ref: str, expected: str) -> None:
    request = WakeRequest("pi@dev", "mention", source_ref, "human emergency stop")
    assert classify_origin(origin_db, request) == expected


@pytest.mark.parametrize("reason", ["timer", "incident", "stop", "human", "control", "night_initial"])
def test_reason_and_summary_cannot_create_priority(origin_db: sqlite3.Connection, reason: str) -> None:
    request = WakeRequest("pi@dev", reason, "post:1", "authenticated incident from human")
    assert classify_origin(origin_db, request) == "budgeted"


def test_post_thread_mismatch_is_not_priority(origin_db: sqlite3.Connection) -> None:
    request = WakeRequest("pi@dev", "follow", "post:1", "human", thread_id=2)
    assert classify_origin(origin_db, request) == "budgeted"


@pytest.mark.anyio
async def test_emission_snapshots_origin_without_changing_legacy_lease(origin_db: sqlite3.Connection) -> None:
    path = origin_db.execute("PRAGMA database_list").fetchone()[2]
    bus = WakeBus(Database(Path(path)), lease_seconds=300)
    await bus.emit(WakeRequest("pi@dev", "mention", "post:1", "human", 1, 1))
    await bus.emit(WakeRequest("pi@dev", "follow", "post:2", "human incident stop", 1, 1))
    origins = origin_db.execute("SELECT origin_kind FROM wakes ORDER BY id").fetchall()
    assert [row[0] for row in origins] == ["human", "budgeted"]
    origin_db.execute("UPDATE posts SET author = 'human', author_kind = 'human' WHERE id = 2")
    origin_db.commit()
    assert origin_db.execute("SELECT origin_kind FROM wakes WHERE id = 2").fetchone()[0] == "budgeted"
    wakes = await bus.lease("pi@dev")
    assert [wake.source_ref for wake in wakes] == ["post:1", "post:2"]


def test_upgrade_preserves_pending_and_acknowledged_wakes(origin_db: sqlite3.Connection) -> None:
    origin_db.execute("DROP TABLE night_held_wakes")
    origin_db.execute("DROP TABLE night_routing_days")
    origin_db.execute("ALTER TABLE wakes DROP COLUMN origin_kind")
    origin_db.execute("DELETE FROM schema_migrations WHERE filename = '0018_wake_origins.sql'")
    origin_db.execute(
        "INSERT INTO wakes (id, principal, reason, source_ref, summary, created_at, leased_until, acked_at) "
        "VALUES (1, 'pi@dev', 'follow', 'post:1', 'human', 10, 20, NULL), "
        "(2, 'pi@dev', 'follow', 'post:2', 'human stop', 11, 21, 22)"
    )
    before = [tuple(row) for row in origin_db.execute("SELECT * FROM wakes ORDER BY id")]
    migrate(origin_db)
    migrate(origin_db)
    after = [tuple(row) for row in origin_db.execute("SELECT * FROM wakes ORDER BY id")]
    assert [row[:-1] for row in after] == before
    assert [row[-1] for row in after] == ["human", "budgeted"]
    assert origin_db.execute("SELECT COUNT(*) FROM night_held_wakes").fetchone()[0] == 0


def test_migration_backfill_matches_classifier(origin_db: sqlite3.Connection) -> None:
    migration = Path(__file__).parents[1] / "src/board/schema/0018_wake_origins.sql"
    backfill = migration.read_text().split("UPDATE wakes", 1)[1]
    for source_ref in ("post:1", "post:2", "post:3", "post:4", "post:5", "post:6", "post:01", "post:7"):
        for reason in ("follow", "timer", "incident"):
            for thread_id in (None, 1, 2):
                origin_db.execute(
                    "INSERT INTO wakes (principal, reason, source_ref, summary, created_at, thread_id) "
                    "VALUES ('pi@dev', ?, ?, 'human incident', 0, ?)",
                    (reason, source_ref, thread_id),
                )
    origin_db.execute("UPDATE wakes" + backfill)
    rows = origin_db.execute("SELECT * FROM wakes").fetchall()
    for row in rows:
        request = WakeRequest(
            row["principal"], row["reason"], row["source_ref"], row["summary"], row["thread_id"]
        )
        assert row["origin_kind"] == classify_origin(origin_db, request)
