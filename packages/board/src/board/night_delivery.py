import json
import sqlite3
from typing import Any

from board.night import Run, hold_wakes_in_transaction

CAPTURE_BATCH = 20
CAPTURE_BYTES = 32768
SUMMARY_CHARS = 1024


class CaptureDenied(ValueError):
    pass


def capture_candidates(conn: sqlite3.Connection, run: Run, now: float) -> list[dict[str, Any]]:
    hold_wakes_in_transaction(conn, now, run.principal)
    if conn.execute(
        "SELECT 1 FROM wakes WHERE principal = ? AND acked_at IS NULL AND origin_kind = 'human' LIMIT 1",
        (run.principal,),
    ).fetchone():
        raise CaptureDenied("human_priority")
    rows = conn.execute(
        "SELECT w.id, w.reason, w.source_ref, w.summary, w.thread_id, w.topic_id "
        "FROM wakes w JOIN night_held_wakes h ON h.wake_id = w.id "
        "WHERE w.principal = ? AND w.acked_at IS NULL AND w.origin_kind = 'budgeted' "
        "AND h.local_date = ? AND (w.leased_until IS NULL OR w.leased_until < ?) "
        "AND NOT EXISTS (SELECT 1 FROM night_captured_wakes cw "
        "JOIN night_wake_captures c ON c.receipt_id = cw.capture_id "
        "JOIN night_runs r ON r.id = c.run_id WHERE cw.wake_id = w.id AND r.state != 'finished') "
        "ORDER BY w.id LIMIT ?",
        (run.principal, run.local_date, now, CAPTURE_BATCH),
    ).fetchall()
    batch: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        summary = item["summary"]
        item["summary"] = summary[:SUMMARY_CHARS]
        item["summary_truncated"] = len(summary) > SUMMARY_CHARS
        if len(json.dumps([*batch, item], separators=(",", ":")).encode()) > CAPTURE_BYTES:
            if not batch:
                raise CaptureDenied("wake_metadata_too_large")
            break
        batch.append(item)
    return batch


def persist_capture(
    conn: sqlite3.Connection, receipt_id: str, run_id: str, now: float, batch: list[dict[str, Any]]
) -> None:
    conn.execute(
        "INSERT INTO night_wake_captures (receipt_id, run_id, captured_at) VALUES (?, ?, ?)",
        (receipt_id, run_id, now),
    )
    conn.executemany(
        "INSERT INTO night_captured_wakes (capture_id, wake_id) VALUES (?, ?)",
        [(receipt_id, item["id"]) for item in batch],
    )
