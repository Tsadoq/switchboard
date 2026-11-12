import base64
import json
import sqlite3
from typing import Any

from board.night import Run

BODY_CHUNK_BYTES = 3072
MAX_BODY_BYTES = 262144
MAX_READS = 128
MAX_TOTAL_BYTES = 524288
MAX_RESPONSE_BYTES = 7168


class ContextDenied(ValueError):
    pass


def read_context(
    conn: sqlite3.Connection,
    run: Run,
    capture_id: str,
    thread_id: int,
    post_id: int | None,
    body_offset: int,
) -> dict[str, Any]:
    if type(thread_id) is not int or not 0 < thread_id < 2**63:
        raise ContextDenied("invalid_thread")
    if post_id is not None and (type(post_id) is not int or not 0 < post_id < 2**63):
        raise ContextDenied("invalid_post")
    if type(body_offset) is not int or not 0 <= body_offset < MAX_BODY_BYTES:
        raise ContextDenied("invalid_body_offset")
    if post_id is None and body_offset != 0:
        raise ContextDenied("missing_post_cursor")
    row = conn.execute(
        "SELECT r.response FROM night_wake_captures c "
        "JOIN night_verifier_receipts r ON r.receipt_id = c.receipt_id "
        "WHERE c.receipt_id = ? AND c.run_id = ?",
        (capture_id, run.id),
    ).fetchone()
    if row is None:
        raise ContextDenied("unknown_capture")
    snapshot = json.loads(row["response"])
    if not any(wake.get("thread_id") == thread_id for wake in snapshot["wakes"]):
        raise ContextDenied("thread_not_captured")
    title = conn.execute("SELECT substr(title, 1, 129) FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if title is None:
        raise ContextDenied("unknown_thread")
    quota = conn.execute(
        "SELECT reads, bytes FROM night_context_reads WHERE run_id = ?", (run.id,)
    ).fetchone()
    if (quota["reads"] if quota is not None else 0) >= MAX_READS:
        raise ContextDenied("context_read_limit")
    rows = conn.execute(
        "SELECT id, substr(author, 1, 129) AS author, substr(author_kind, 1, 33) AS author_kind, created_at "
        "FROM posts WHERE thread_id = ? AND (? IS NULL OR id <= ?) ORDER BY id DESC LIMIT 2",
        (thread_id, post_id, post_id),
    ).fetchall()
    if post_id is not None and (not rows or rows[0]["id"] != post_id):
        raise ContextDenied("post_not_in_thread")
    post = None
    if rows:
        first = rows[0]
        with conn.blobopen("posts", "body", first["id"], readonly=True) as blob:
            total_bytes = len(blob)
            if body_offset > total_bytes:
                raise ContextDenied("invalid_body_offset")
            blob.seek(body_offset)
            body = blob.read(min(BODY_CHUNK_BYTES, MAX_BODY_BYTES - body_offset))
        end = body_offset + len(body)
        post = {
            "id": first["id"],
            "author": first["author"][:128],
            "author_truncated": len(first["author"]) > 128,
            "author_kind": first["author_kind"][:32],
            "author_kind_truncated": len(first["author_kind"]) > 32,
            "created_at": first["created_at"],
            "body_base64": base64.b64encode(body).decode("ascii"),
            "body_offset": body_offset,
            "body_total_bytes": total_bytes,
            "next_body_offset": end if end < min(total_bytes, MAX_BODY_BYTES) else None,
            "body_end_reached": end == total_bytes,
            "body_limit_reached": end < total_bytes and end == MAX_BODY_BYTES,
        }
    response = {
        "run_id": run.id,
        "capture_id": capture_id,
        "thread_id": thread_id,
        "title": title[0][:128],
        "title_truncated": len(title[0]) > 128,
        "post": post,
        "previous_post_id": rows[1]["id"] if len(rows) > 1 else None,
    }
    size = len(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
    if size > MAX_RESPONSE_BYTES:
        raise ContextDenied("context_response_too_large")
    if size + (quota["bytes"] if quota is not None else 0) > MAX_TOTAL_BYTES:
        raise ContextDenied("context_byte_limit")
    conn.execute(
        "INSERT INTO night_context_reads (run_id, reads, bytes) VALUES (?, 1, ?) "
        "ON CONFLICT (run_id) DO UPDATE SET reads = reads + 1, bytes = bytes + excluded.bytes",
        (run.id, size),
    )
    return response
