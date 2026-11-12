import base64
import binascii
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agent_board.night.ledger import CapturedAssignment, Ledger
from agent_board.night.sync import SyncError
from agent_board.night_state import Record

BODY_CHUNK_BYTES = 3072
MAX_BODY_BYTES = 262144
MAX_RESPONSE_BYTES = 7168
MAX_TITLE_CHARS = 128
MAX_AUTHOR_CHARS = 128
MAX_KIND_CHARS = 32
MAX_ID = 2**63 - 1
_RESPONSE_KEYS = frozenset(
    ("run_id", "capture_id", "thread_id", "title", "title_truncated", "post", "previous_post_id")
)
_POST_KEYS = frozenset(
    (
        "id",
        "author",
        "author_truncated",
        "author_kind",
        "author_kind_truncated",
        "created_at",
        "body_base64",
        "body_offset",
        "body_total_bytes",
        "next_body_offset",
        "body_end_reached",
        "body_limit_reached",
    )
)
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")


class ContextRefused(SyncError):
    """A context read this verifier will not use: malformed, mismatched, or not a contiguous body."""


class ContextBoard(Protocol):
    def thread_read(self, run_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class Chunk:
    post_id: int
    author: str
    author_truncated: bool
    author_kind: str
    author_kind_truncated: bool
    created_at: float
    body: bytes
    body_offset: int
    body_total_bytes: int
    next_body_offset: int | None
    body_end_reached: bool
    body_limit_reached: bool


@dataclass(frozen=True)
class Page:
    thread_id: int
    title: str
    title_truncated: bool
    chunk: Chunk | None
    previous_post_id: int | None


@dataclass(frozen=True)
class Post:
    """One post's body, reassembled from offset zero. `text` is set only when every byte was read."""

    post_id: int
    author: str
    author_kind: str
    created_at: float
    body: bytes
    body_total_bytes: int
    complete: bool
    text: str | None
    previous_post_id: int | None


class ContextReader:
    """Reads captured threads for one bound run through the verifier's signed endpoint.

    Every authority value comes from the journal record and the ledger, never from a caller: the caller
    names only a thread, a post and a byte offset. A read never moves the ledger counter, acknowledges a
    wake, or authorizes anything; it is data quoted to the task.
    """

    def __init__(self, board: ContextBoard, ledger: Ledger) -> None:
        self._board = board
        self._ledger = ledger

    def page(self, record: Record, thread_id: int, post_id: int | None, body_offset: int) -> Page:
        if record.state != "starting" or record.stop_requested or record.uncertain:
            raise ContextRefused("run_not_running")
        capture = self._ledger.captured(record.run_id)
        if not isinstance(capture, CapturedAssignment) or capture.aborted_after_apply:
            raise ContextRefused("no_usable_capture")
        if not _id(thread_id) or thread_id not in _captured_threads(capture.snapshot):
            raise ContextRefused("thread_not_captured")
        if post_id is not None and not _id(post_id):
            raise ContextRefused("invalid_post")
        if type(body_offset) is not int or not 0 <= body_offset < MAX_BODY_BYTES:
            raise ContextRefused("invalid_body_offset")
        if post_id is None and body_offset != 0:
            raise ContextRefused("missing_post_cursor")
        binding = self._ledger.binding(record.run_id)
        journal_id = self._ledger.identity().journal_id
        if binding is None or not binding.server_acked or journal_id is None:
            raise ContextRefused("not_bound")
        data = self._board.thread_read(
            record.run_id,
            {
                "fence": record.fence,
                "journal_id": journal_id,
                "boot_id": binding.boot_id,
                "invocation_id": binding.invocation_id,
                "cgroup": binding.cgroup,
                "capture_id": capture.receipt_id,
                "thread_id": thread_id,
                "post_id": post_id,
                "body_offset": body_offset,
            },
        )
        return parse_page(
            data,
            run_id=record.run_id,
            capture_id=capture.receipt_id,
            thread_id=thread_id,
            post_id=post_id,
            body_offset=body_offset,
        )

    def post(self, record: Record, thread_id: int, post_id: int | None) -> Post | None:
        """The whole post, reading contiguous chunks from offset zero; None for an empty thread."""
        first = self.page(record, thread_id, post_id, 0)
        if first.chunk is None:
            return None
        chunk = first.chunk
        body = bytearray(chunk.body)
        while chunk.next_body_offset is not None:
            following = self.page(record, thread_id, chunk.post_id, chunk.next_body_offset)
            if following.chunk is None or following.chunk.post_id != chunk.post_id:
                raise ContextRefused("post_changed_while_reading")
            if following.chunk.body_offset != len(body):
                raise ContextRefused("body_not_contiguous")
            chunk = following.chunk
            body.extend(chunk.body)
        complete = chunk.body_end_reached and len(body) == chunk.body_total_bytes
        text = None
        if complete:
            try:
                text = bytes(body).decode("utf-8")
            except UnicodeDecodeError:
                raise ContextRefused("body_not_utf8") from None
        return Post(
            post_id=chunk.post_id,
            author=chunk.author,
            author_kind=chunk.author_kind,
            created_at=chunk.created_at,
            body=bytes(body),
            body_total_bytes=chunk.body_total_bytes,
            complete=complete,
            text=text,
            previous_post_id=first.previous_post_id,
        )


def parse_page(
    data: Mapping[str, Any],
    *,
    run_id: str,
    capture_id: str,
    thread_id: int,
    post_id: int | None,
    body_offset: int,
) -> Page:
    if not isinstance(data, Mapping) or frozenset(data) != _RESPONSE_KEYS:
        raise ContextRefused("context_fields")
    try:
        size = len(
            json.dumps(dict(data), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        )
    except (TypeError, ValueError, RecursionError):
        raise ContextRefused("context_not_json") from None
    if size > MAX_RESPONSE_BYTES:
        raise ContextRefused("context_response_too_large")
    if data["run_id"] != run_id or data["capture_id"] != capture_id or data["thread_id"] != thread_id:
        raise ContextRefused("context_mismatch")
    if type(data["thread_id"]) is not int:
        raise ContextRefused("context_mismatch")
    title, title_truncated = _bounded_text(data["title"], data["title_truncated"], MAX_TITLE_CHARS)
    previous = data["previous_post_id"]
    if previous is not None and not _id(previous):
        raise ContextRefused("invalid_previous_post")
    post = data["post"]
    if post is None:
        if post_id is not None or previous is not None:
            raise ContextRefused("context_mismatch")
        return Page(thread_id, title, title_truncated, None, None)
    chunk = _chunk(post, post_id=post_id, body_offset=body_offset)
    if previous is not None and previous >= chunk.post_id:
        raise ContextRefused("invalid_previous_post")
    return Page(thread_id, title, title_truncated, chunk, previous)


def _chunk(post: Any, *, post_id: int | None, body_offset: int) -> Chunk:
    if not isinstance(post, Mapping) or frozenset(post) != _POST_KEYS:
        raise ContextRefused("context_post_fields")
    if not _id(post["id"]) or (post_id is not None and post["id"] != post_id):
        raise ContextRefused("context_mismatch")
    author, author_truncated = _bounded_text(post["author"], post["author_truncated"], MAX_AUTHOR_CHARS)
    kind, kind_truncated = _bounded_text(post["author_kind"], post["author_kind_truncated"], MAX_KIND_CHARS)
    created = post["created_at"]
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created):
        raise ContextRefused("invalid_created_at")
    encoded = post["body_base64"]
    if not isinstance(encoded, str) or len(encoded) % 4 or _BASE64.fullmatch(encoded) is None:
        raise ContextRefused("invalid_body_base64")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ContextRefused("invalid_body_base64") from None
    offset, total, following = post["body_offset"], post["body_total_bytes"], post["next_body_offset"]
    end_reached, limit_reached = post["body_end_reached"], post["body_limit_reached"]
    if (
        type(offset) is not int
        or offset != body_offset
        or type(total) is not int
        or total < 0
        or offset > total
        or (following is not None and type(following) is not int)
        or type(end_reached) is not bool
        or type(limit_reached) is not bool
    ):
        raise ContextRefused("invalid_body_cursor")
    end = offset + len(body)
    expected_length = min(BODY_CHUNK_BYTES, MAX_BODY_BYTES - offset, total - offset)
    if (
        len(body) != expected_length
        or following != (end if end < min(total, MAX_BODY_BYTES) else None)
        or end_reached != (end == total)
        or limit_reached != (end < total and end == MAX_BODY_BYTES)
    ):
        raise ContextRefused("invalid_body_cursor")
    return Chunk(
        post_id=post["id"],
        author=author,
        author_truncated=author_truncated,
        author_kind=kind,
        author_kind_truncated=kind_truncated,
        created_at=float(created),
        body=body,
        body_offset=offset,
        body_total_bytes=total,
        next_body_offset=following,
        body_end_reached=end_reached,
        body_limit_reached=limit_reached,
    )


def _bounded_text(value: Any, truncated: Any, limit: int) -> tuple[str, bool]:
    if not isinstance(value, str) or len(value) > limit or type(truncated) is not bool:
        raise ContextRefused("invalid_metadata")
    return value, truncated


def _captured_threads(snapshot: str) -> frozenset[int]:
    try:
        wakes = json.loads(snapshot)["wakes"]
    except (ValueError, KeyError, TypeError):
        raise ContextRefused("capture_snapshot_unreadable") from None
    return frozenset(
        wake["thread_id"]
        for wake in wakes
        if isinstance(wake, dict) and type(wake.get("thread_id")) is int and wake["thread_id"] > 0
    )


def _id(value: Any) -> bool:
    return type(value) is int and 0 < value <= MAX_ID
