import shutil
from contextlib import closing
from pathlib import Path

from board.db import SCHEMA_DIR, connect, migrate


def test_last_post_is_backfilled_and_kept_by_triggers(tmp_path: Path) -> None:
    old_schema = tmp_path / "old-schema"
    old_schema.mkdir()
    for script in sorted(SCHEMA_DIR.glob("*.sql")):
        if script.name < "0014":
            shutil.copyfile(script, old_schema / script.name)
    with closing(connect(tmp_path / "board.db")) as conn:
        migrate(conn, old_schema)
        conn.execute("INSERT INTO subforums (slug, title, description) VALUES ('general', 'General', '')")
        conn.execute(
            "INSERT INTO threads (id, subforum, title, kind, created_by, created_at) "
            "VALUES (1, 'general', 'Discussion', 'discussion', 'human', 1000)"
        )
        conn.execute("INSERT INTO principals (principal, last_poll_at) VALUES ('pi@box', 5)")
        conn.executemany(
            "INSERT INTO posts (id, thread_id, author, author_kind, body, created_at) "
            "VALUES (?, 1, ?, 'agent', 'Post', 100)",
            [(1, "claude@box"), (2, "pi@box"), (3, "claude@box")],
        )
        migrate(conn)

        def last_posts() -> dict[str, int | None]:
            return {
                row["principal"]: row["last_post_id"]
                for row in conn.execute("SELECT principal, last_post_id FROM principals ORDER BY principal")
            }

        assert last_posts() == {"claude@box": 3, "pi@box": 2}
        assert (
            conn.execute("SELECT last_poll_at FROM principals WHERE principal = 'pi@box'").fetchone()[0] == 5
        )
        conn.execute(
            "INSERT INTO posts (id, thread_id, author, author_kind, body, created_at) "
            "VALUES (4, 1, 'pi@box', 'agent', 'New', 200)"
        )
        assert last_posts() == {"claude@box": 3, "pi@box": 4}
        conn.execute("DELETE FROM posts WHERE id = 4")
        assert last_posts() == {"claude@box": 3, "pi@box": 2}
        conn.execute("DELETE FROM posts WHERE id = 1")
        assert last_posts() == {"claude@box": 3, "pi@box": 2}
        conn.execute("DELETE FROM posts WHERE author = 'claude@box'")
        assert last_posts() == {"claude@box": None, "pi@box": 2}
