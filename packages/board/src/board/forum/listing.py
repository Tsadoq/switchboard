import re
import sqlite3

from board.forum.entities import ListPage


class InvalidCursor(ValueError):
    pass


def names(before: str | None, after: str | None, *, agent: bool = False) -> None:
    if before is not None and after is not None:
        raise InvalidCursor
    pattern = r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+" if agent else r"[a-z0-9][a-z0-9-]*"
    if any(key is not None and re.fullmatch(pattern, key) is None for key in (before, after)):
        raise InvalidCursor


def rooms(conn: sqlite3.Connection, before: str | None, after: str | None) -> ListPage[sqlite3.Row]:
    names(before, after)
    key = before if before is not None else after
    order = "DESC" if before is not None else "ASC"
    operator = "<" if before is not None else ">"
    bound = f" WHERE slug {operator} ?" if key is not None else ""
    rows = tuple(
        conn.execute(
            f"SELECT * FROM subforums{bound} ORDER BY slug {order} LIMIT 50",
            () if key is None else (key,),
        )
    )
    if before is not None:
        rows = tuple(reversed(rows))
    total = conn.execute("SELECT COUNT(*) FROM subforums").fetchone()[0]
    older = (
        rows[-1]["slug"]
        if rows
        and conn.execute("SELECT 1 FROM subforums WHERE slug > ? LIMIT 1", (rows[-1]["slug"],)).fetchone()
        else None
    )
    newer = (
        rows[0]["slug"]
        if rows
        and conn.execute("SELECT 1 FROM subforums WHERE slug < ? LIMIT 1", (rows[0]["slug"],)).fetchone()
        else None
    )
    return ListPage(rows, total, older, newer)


def agents(
    conn: sqlite3.Connection, roster: str, mention: str, before: str | None, after: str | None
) -> ListPage[sqlite3.Row]:
    names(before, after, agent=True)
    base = "WITH roster AS (SELECT value AS principal FROM json_each(?)) "
    where = "(principal = ? OR substr(principal, 1, instr(principal, '@') - 1) = ?)" if mention else "1 = 1"
    params = (roster, mention, mention) if mention else (roster,)
    key = before if before is not None else after
    order = "DESC" if before is not None else "ASC"
    operator = "<" if before is not None else ">"
    bound = f" AND principal {operator} ?" if key is not None else ""
    rows = tuple(
        conn.execute(
            base + f", candidates AS (SELECT principal FROM roster WHERE {where}{bound} "
            f"ORDER BY principal {order} LIMIT 50) "
            "SELECT c.principal, p.display_name, p.specialities, p.wake_every_seconds, r.last_seen_at, r.last_poll_at "
            "FROM candidates c LEFT JOIN profiles p ON p.principal = c.principal "
            f"LEFT JOIN principals r ON r.principal = c.principal ORDER BY c.principal {order}",
            (*params, *((key,) if key is not None else ())),
        )
    )
    if before is not None:
        rows = tuple(reversed(rows))
    total = conn.execute(base + f"SELECT COUNT(*) FROM roster WHERE {where}", params).fetchone()[0]

    def exists(value: str, comparison: str) -> bool:
        return (
            conn.execute(
                base + f"SELECT 1 FROM roster WHERE {where} AND principal {comparison} ? LIMIT 1",
                (*params, value),
            ).fetchone()
            is not None
        )

    older = rows[-1]["principal"] if rows and exists(rows[-1]["principal"], ">") else None
    newer = rows[0]["principal"] if rows and exists(rows[0]["principal"], "<") else None
    return ListPage(rows, total, older, newer)


def mentions(
    conn: sqlite3.Connection, principal: str, before: int | None, after: int | None, archived: bool
) -> ListPage[sqlite3.Row]:
    if before is not None and after is not None:
        raise InvalidCursor
    if any(key is not None and not 0 <= key <= 2**63 - 1 for key in (before, after)):
        raise InvalidCursor
    source = (
        "FROM mentions m JOIN posts p ON p.id = m.post_id JOIN threads t ON t.id = p.thread_id "
        "LEFT JOIN mention_reads r ON r.post_id = m.post_id AND r.principal = m.principal "
        "WHERE m.principal = ? AND (r.post_id IS NOT NULL) = ?"
    )
    key = before or after
    order = "ASC" if after is not None else "DESC"
    operator = ">" if after is not None else "<"
    bound = f" AND p.id {operator} ?" if key is not None else ""
    rows = tuple(
        conn.execute(
            f"SELECT p.*, t.title {source}{bound} ORDER BY p.id {order} LIMIT 50",
            (principal, archived, *((key,) if key is not None else ())),
        )
    )
    if after is not None:
        rows = tuple(reversed(rows))
    total = conn.execute(f"SELECT COUNT(*) {source}", (principal, archived)).fetchone()[0]

    def exists(value: int, comparison: str) -> bool:
        return (
            conn.execute(
                f"SELECT 1 {source} AND p.id {comparison} ? LIMIT 1", (principal, archived, value)
            ).fetchone()
            is not None
        )

    older = str(rows[-1]["id"]) if rows and exists(rows[-1]["id"], "<") else None
    newer = str(rows[0]["id"]) if rows and exists(rows[0]["id"], ">") else None
    return ListPage(rows, total, older, newer)
