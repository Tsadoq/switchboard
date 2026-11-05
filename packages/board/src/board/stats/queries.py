import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

from board.files import LIVE_CONDITION
from board.tickets import COLUMNS

WINDOW_DAYS = 30
WAKE_WINDOW_DAYS = 7
TOP_MENTIONED_LIMIT = 10
_DAY_SECONDS = 86400
_NEAREST_RANK_PERCENTILES = (
    "WITH acked AS ("
    "SELECT acked_at - created_at AS latency, "
    "ROW_NUMBER() OVER (ORDER BY acked_at - created_at) AS position, COUNT(*) OVER () AS total "
    "FROM wakes WHERE acked_at IS NOT NULL AND created_at >= ?) "
    "SELECT MIN(CASE WHEN position * 100 >= total * 50 THEN latency END) AS p50, "
    "MIN(CASE WHEN position * 100 >= total * 95 THEN latency END) AS p95 FROM acked"
)


class CycleTime(TypedDict):
    ticket_id: int
    title: str
    days: float


class WakeLatency(TypedDict):
    p50: float | None
    p95: float | None


class UploaderBytes(TypedDict):
    uploader: str
    bytes: int


class MentionCount(TypedDict):
    principal: str
    mentions: int


class Aggregates(TypedDict):
    posts_per_principal_per_day: dict[str, dict[str, int]]
    tickets_per_column: dict[str, int]
    ticket_cycle_time_days: list[CycleTime]
    wake_latency_seconds: WakeLatency
    files_live_bytes_per_uploader: list[UploaderBytes]
    top_mentioned: list[MentionCount]


def aggregates(conn: sqlite3.Connection) -> Aggregates:
    now = time.time()
    return {
        "posts_per_principal_per_day": posts_per_principal_per_day(conn, now),
        "tickets_per_column": tickets_per_column(conn),
        "ticket_cycle_time_days": ticket_cycle_time_days(conn, now),
        "wake_latency_seconds": wake_latency_seconds(conn, now),
        "files_live_bytes_per_uploader": files_live_bytes_per_uploader(conn, now),
        "top_mentioned": top_mentioned(conn, now),
    }


def posts_per_principal_per_day(
    conn: sqlite3.Connection, now: float, days: int = WINDOW_DAYS
) -> dict[str, dict[str, int]]:
    first_day = _first_day(now, days)
    rows = conn.execute(
        "SELECT date(created_at, 'unixepoch') AS day, author, COUNT(*) AS posts FROM posts "
        "WHERE created_at >= ? GROUP BY day, author ORDER BY day, author",
        (_epoch(first_day),),
    )
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["day"], {})[row["author"]] = row["posts"]
    labels = [(first_day + timedelta(days=offset)).isoformat() for offset in range(days)]
    return {day: counts.get(day, {}) for day in labels}


def tickets_per_column(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute('SELECT "column", COUNT(*) AS tickets FROM tickets GROUP BY "column"')
    counts = {row["column"]: row["tickets"] for row in rows}
    return {column: counts.get(column, 0) for column in COLUMNS}


def ticket_cycle_time_days(conn: sqlite3.Connection, now: float, days: int = WINDOW_DAYS) -> list[CycleTime]:
    rows = conn.execute(
        "SELECT id, title, (done_at - created_at) / ? AS days FROM tickets "
        "WHERE done_at IS NOT NULL AND done_at >= ? ORDER BY done_at, id",
        (_DAY_SECONDS, _epoch(_first_day(now, days))),
    )
    return [{"ticket_id": row["id"], "title": row["title"], "days": row["days"]} for row in rows]


def wake_latency_seconds(conn: sqlite3.Connection, now: float, days: int = WAKE_WINDOW_DAYS) -> WakeLatency:
    row = conn.execute(_NEAREST_RANK_PERCENTILES, (_epoch(_first_day(now, days)),)).fetchone()
    return {"p50": row["p50"], "p95": row["p95"]}


def files_live_bytes_per_uploader(conn: sqlite3.Connection, now: float) -> list[UploaderBytes]:
    rows = conn.execute(
        f"SELECT uploaded_by, SUM(size) AS bytes FROM files WHERE {LIVE_CONDITION} "
        "GROUP BY uploaded_by ORDER BY bytes DESC, uploaded_by",
        (now,),
    )
    return [{"uploader": row["uploaded_by"], "bytes": row["bytes"]} for row in rows]


def top_mentioned(
    conn: sqlite3.Connection, now: float, days: int = WINDOW_DAYS, limit: int = TOP_MENTIONED_LIMIT
) -> list[MentionCount]:
    rows = conn.execute(
        "SELECT m.principal, COUNT(*) AS mentions FROM mentions m JOIN posts p ON p.id = m.post_id "
        "WHERE p.created_at >= ? GROUP BY m.principal ORDER BY mentions DESC, m.principal LIMIT ?",
        (_epoch(_first_day(now, days)), limit),
    )
    return [{"principal": row["principal"], "mentions": row["mentions"]} for row in rows]


def _first_day(now: float, days: int) -> date:
    return datetime.fromtimestamp(now, UTC).date() - timedelta(days=days - 1)


def _epoch(day: date) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()
