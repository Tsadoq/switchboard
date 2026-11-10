import base64
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from board.stats import queries


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class Keys:
    values: tuple[str, ...]
    total: int
    next: str | None
    previous: str | None

    def metadata(self) -> dict[str, Any]:
        return {"total": self.total, "shown": len(self.values), "next": self.next, "previous": self.previous}


def aggregates(conn: sqlite3.Connection, cursors: dict[str, tuple[str | None, str | None]]) -> dict[str, Any]:
    now = time.time()
    first = datetime.fromtimestamp(now, UTC).date() - timedelta(days=queries.WINDOW_DAYS - 1)
    cutoff = datetime(first.year, first.month, first.day, tzinfo=UTC).timestamp()
    authors = _keys(conn, "posts", "author", "created_at >= ?", cutoff, *cursors["posts"])
    uploaders = _keys(conn, "files", "uploaded_by", "expires_at > ?", now, *cursors["files"])
    days: dict[str, dict[str, int]] = {
        (first + timedelta(days=i)).isoformat(): {} for i in range(queries.WINDOW_DAYS)
    }
    rows = conn.execute(
        "SELECT date(created_at, 'unixepoch') AS day, author, COUNT(*) AS posts FROM posts "
        "WHERE created_at >= ? AND author IN (SELECT value FROM json_each(?)) GROUP BY day, author",
        (cutoff, json.dumps(authors.values)),
    )
    for row in rows:
        if row["day"] in days:
            days[row["day"]][row["author"]] = row["posts"]
    files = [
        {"uploader": row["uploaded_by"], "bytes": row["bytes"]}
        for row in conn.execute(
            "SELECT uploaded_by, SUM(size) AS bytes FROM files WHERE expires_at > ? "
            "AND uploaded_by IN (SELECT value FROM json_each(?)) GROUP BY uploaded_by ORDER BY uploaded_by",
            (now, json.dumps(uploaders.values)),
        )
    ]
    tickets, ticket_meta = _tickets(conn, cutoff, *cursors["tickets"])
    return {
        "posts_per_principal_per_day": days,
        "tickets_per_column": queries.tickets_per_column(conn),
        "ticket_cycle_time_days": tickets,
        "wake_latency_seconds": queries.wake_latency_seconds(conn, now),
        "files_live_bytes_per_uploader": files,
        "top_mentioned": queries.top_mentioned(conn, now),
        "pages": {"posts": authors.metadata(), "files": uploaders.metadata(), "tickets": ticket_meta},
    }


def _validate(before: str | None, after: str | None) -> None:
    if before is not None and after is not None or before == "" or after == "":
        raise InvalidCursor


def _keys(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    where: str,
    cutoff: float,
    before: str | None,
    after: str | None,
) -> Keys:
    _validate(before, after)
    key = before if before is not None else after
    order = "DESC" if before is not None else "ASC"
    operator = "<" if before is not None else ">"
    bound = f" AND {column} {operator} ?" if key is not None else ""
    values = tuple(
        row[0]
        for row in conn.execute(
            f"SELECT {column} FROM {table} WHERE {where}{bound} GROUP BY {column} "
            f"ORDER BY {column} {order} LIMIT 50",
            (cutoff, *((key,) if key is not None else ())),
        )
    )
    if before is not None:
        values = tuple(reversed(values))
    total = conn.execute(f"SELECT COUNT(DISTINCT {column}) FROM {table} WHERE {where}", (cutoff,)).fetchone()[
        0
    ]

    def exists(value: str, comparison: str) -> bool:
        return (
            conn.execute(
                f"SELECT 1 FROM {table} WHERE {where} AND {column} {comparison} ? LIMIT 1", (cutoff, value)
            ).fetchone()
            is not None
        )

    next_key = values[-1] if values and exists(values[-1], ">") else None
    previous = values[0] if values and exists(values[0], "<") else None
    return Keys(values, total, next_key, previous)


def _tickets(
    conn: sqlite3.Connection, cutoff: float, before: str | None, after: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _validate(before, after)
    token = before if before is not None else after
    key = _decode(token) if token is not None else None
    order = "ASC" if after is not None else "DESC"
    operator = ">" if after is not None else "<"
    bound = f" AND (done_at, id) {operator} (?, ?)" if key is not None else ""
    rows = list(
        conn.execute(
            f"SELECT id, title, created_at, done_at FROM tickets WHERE done_at >= ?{bound} "
            f"ORDER BY done_at {order}, id {order} LIMIT 50",
            (cutoff, *(key or ())),
        )
    )
    if after is not None:
        rows.reverse()
    total = conn.execute("SELECT COUNT(*) FROM tickets WHERE done_at >= ?", (cutoff,)).fetchone()[0]

    def exists(row: sqlite3.Row, comparison: str) -> bool:
        return (
            conn.execute(
                f"SELECT 1 FROM tickets WHERE done_at >= ? AND (done_at, id) {comparison} (?, ?) LIMIT 1",
                (cutoff, row["done_at"], row["id"]),
            ).fetchone()
            is not None
        )

    next_key = _encode(rows[-1]) if rows and exists(rows[-1], "<") else None
    previous = _encode(rows[0]) if rows and exists(rows[0], ">") else None
    items = [
        {"ticket_id": row["id"], "title": row["title"], "days": (row["done_at"] - row["created_at"]) / 86400}
        for row in rows
    ]
    return items, {"total": total, "shown": len(rows), "next": next_key, "previous": previous}


def _encode(row: sqlite3.Row) -> str:
    return base64.urlsafe_b64encode(json.dumps([row["done_at"], row["id"]]).encode()).decode().rstrip("=")


def _decode(token: str) -> tuple[float, int]:
    try:
        if len(token) > 512:
            raise ValueError
        values = json.loads(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True))
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError
        moment, ticket_id = values
        if type(moment) not in (int, float) or not math.isfinite(moment) or moment < 0:
            raise ValueError
        if type(ticket_id) is not int or not 1 <= ticket_id <= 2**63 - 1:
            raise ValueError
        return float(moment), ticket_id
    except (ValueError, TypeError, OverflowError):
        raise InvalidCursor from None
