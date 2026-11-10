import shutil
from contextlib import closing
from pathlib import Path

from board.db import SCHEMA_DIR, connect, migrate


def test_thread_activity_backfill_and_trigger_mutations(tmp_path: Path) -> None:
    old_schema = tmp_path / "old-schema"
    old_schema.mkdir()
    for script in sorted(SCHEMA_DIR.glob("*.sql")):
        if script.name < "0011":
            shutil.copyfile(script, old_schema / script.name)
    with closing(connect(tmp_path / "board.db")) as conn:
        migrate(conn, old_schema)
        conn.execute("INSERT INTO subforums (slug, title, description) VALUES ('general', 'General', '')")
        conn.executemany(
            "INSERT INTO threads (id, subforum, title, kind, created_by, created_at) "
            "VALUES (?, 'general', 'Discussion', 'discussion', 'human', 1000)",
            [(1,), (2,), (3,)],
        )
        conn.executemany(
            "INSERT INTO posts (id, thread_id, author, author_kind, body, created_at) "
            "VALUES (?, ?, 'human', 'human', 'Post', ?)",
            [(1, 1, 100), (2, 1, 200), (3, 3, 50)],
        )
        migrate(conn)

        def activity() -> dict[int, float | None]:
            rows = conn.execute("SELECT id, last_post_at FROM threads ORDER BY id")
            cached = {row["id"]: row["last_post_at"] for row in rows}
            expected = {
                row["id"]: row["activity"]
                for row in conn.execute(
                    "SELECT id, (SELECT MAX(created_at) FROM posts WHERE thread_id = threads.id) AS activity FROM threads"
                )
            }
            assert cached == expected
            return cached

        assert activity() == {1: 200, 2: None, 3: 50}
        conn.execute(
            "INSERT INTO posts (id, thread_id, author, author_kind, body, created_at) "
            "VALUES (4, 1, 'human', 'human', 'Backdated', 90)"
        )
        assert activity()[1] == 200
        conn.execute("UPDATE posts SET created_at = 300 WHERE id = 4")
        assert activity()[1] == 300
        conn.execute("UPDATE posts SET thread_id = 3 WHERE id = 4")
        assert activity() == {1: 200, 2: None, 3: 300}
        conn.execute("DELETE FROM posts WHERE id = 4")
        assert activity()[3] == 50
        conn.execute("DELETE FROM posts WHERE thread_id = 1")
        assert activity()[1] is None
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO posts (thread_id, author, author_kind, body, created_at) "
            "VALUES (2, 'human', 'human', 'Rolled back', 400)"
        )
        assert activity()[2] == 400
        conn.execute("ROLLBACK")
        assert activity()[2] is None
        migrate(conn)
        assert activity() == {1: None, 2: None, 3: 50}
