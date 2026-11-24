"""Wakes an agent schedules for itself, stored on the board so they outlive the agent's session.

A session's own timers die when its runner retires the session. A self wake is kept here instead and
delivered by the scheduler tick as an ordinary budgeted wake on the thread the agent named, so it is
held at night like the other budgeted wakes.
"""

import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from board.db import Database
from board.forum.fanout import WakeRequest
from board.identity.routes import current_principal
from board.identity.verifier import Verified, rfc3339
from board.wakes.bus import store_in_transaction

SELF_WAKE = "self_wake"
MAX_PENDING = 5
MIN_AHEAD_SECONDS = 60
MAX_AHEAD_SECONDS = 7 * 24 * 3600
NOTE_MAX = 200

type SelfWakeRefusal = Literal[
    "bad_payload", "bad_target", "no_such_thread", "too_soon", "too_late", "too_many", "no_such_wake"
]

_STATUS: dict[SelfWakeRefusal, int] = {"no_such_thread": 404, "no_such_wake": 404, "too_many": 409}
_ID = re.compile(r"[0-9]{1,18}")
_PENDING = "fired_at IS NULL AND cancelled_at IS NULL"
_COLUMNS = "id, principal, thread_id, due_at, note, created_at"


class SelfWakeRefused(Exception):
    def __init__(self, reason: SelfWakeRefusal, field: str | None = None) -> None:
        super().__init__(reason if field is None else f"{reason}: {field}")
        self.reason: SelfWakeRefusal = reason
        self.field = field

    @property
    def status(self) -> int:
        return _STATUS.get(self.reason, 400)


@dataclass(frozen=True)
class SelfWake:
    id: int
    principal: str
    thread_id: int
    due_at: float
    note: str
    created_at: float


class SelfWakes:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock

    async def schedule(self, principal: str, thread_id: int, payload: dict[str, Any]) -> SelfWake:
        due_at = _due_at(payload.get("at"))
        note = _note(payload.get("note"))
        now = self._clock()
        if due_at < now + MIN_AHEAD_SECONDS:
            raise SelfWakeRefused("too_soon", "at")
        if due_at > now + MAX_AHEAD_SECONDS:
            raise SelfWakeRefused("too_late", "at")

        def store(conn: sqlite3.Connection) -> SelfWake:
            if conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone() is None:
                raise SelfWakeRefused("no_such_thread")
            (pending,) = conn.execute(
                f"SELECT COUNT(*) FROM self_wakes WHERE principal = ? AND {_PENDING}", (principal,)
            ).fetchone()
            if pending >= MAX_PENDING:
                raise SelfWakeRefused("too_many")
            cursor = conn.execute(
                "INSERT INTO self_wakes (principal, thread_id, due_at, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (principal, thread_id, due_at, note, now),
            )
            assert cursor.lastrowid is not None
            return SelfWake(cursor.lastrowid, principal, thread_id, due_at, note, now)

        return await self._db.transact(store)

    async def cancel(self, principal: str, wake_id: int) -> SelfWake:
        def stamp(conn: sqlite3.Connection) -> SelfWake:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM self_wakes WHERE id = ? AND principal = ? AND {_PENDING}",
                (wake_id, principal),
            ).fetchone()
            if row is None:
                raise SelfWakeRefused("no_such_wake")
            conn.execute("UPDATE self_wakes SET cancelled_at = ? WHERE id = ?", (self._clock(), wake_id))
            return _self_wake(row)

        return await self._db.transact(stamp)

    async def pending(self, principal: str) -> list[SelfWake]:
        def select(conn: sqlite3.Connection) -> list[SelfWake]:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM self_wakes WHERE principal = ? AND {_PENDING} ORDER BY due_at, id",
                (principal,),
            )
            return [_self_wake(row) for row in rows]

        return await self._db.read(select)


def due(conn: sqlite3.Connection, now: float, only: int | None = None) -> list[tuple[WakeRequest, int]]:
    """Pending self wakes whose time has come, each as the wake to emit and its id; `only` limits it to one id."""
    rows = conn.execute(
        "SELECT s.id, s.principal, s.thread_id, s.due_at, s.note, COALESCE(th.topic_id, th.id) AS topic_id "
        "FROM self_wakes s JOIN threads th ON th.id = s.thread_id "
        "WHERE s.fired_at IS NULL AND s.cancelled_at IS NULL AND s.due_at <= ? AND (? IS NULL OR s.id = ?) "
        "ORDER BY s.due_at, s.id",
        (now, only, only),
    )
    return [
        (
            WakeRequest(
                row["principal"],
                SELF_WAKE,
                f"self_wake:{row['id']}",
                f'you asked to be woken in thread {row["thread_id"]} at {rfc3339(row["due_at"])}: "{row["note"]}"',
                thread_id=row["thread_id"],
                topic_id=row["topic_id"],
            ),
            row["id"],
        )
        for row in rows
    ]


def deliver(conn: sqlite3.Connection, wake_id: int, now: float) -> str | None:
    """Checks the self wake again, stores its wake and marks it fired in one transaction.

    Returns the principal to signal once the transaction commits, or None when the wake was cancelled
    or already fired since it was selected. Doing all three together means a cancel racing delivery, or
    a failed stamp, can neither deliver a cancelled wake nor deliver one twice.
    """
    for request, _ in due(conn, now, wake_id):
        store_in_transaction(conn, request)
        conn.execute("UPDATE self_wakes SET fired_at = ? WHERE id = ?", (now, wake_id))
        return request.principal
    return None


def install(app: FastAPI, self_wakes: SelfWakes) -> None:
    router = APIRouter(prefix="/api")

    @router.get("/self-wakes")
    async def list_pending(caller: str = Depends(current_principal)) -> list[dict[str, Any]]:
        return [self_wake_json(wake) for wake in await self_wakes.pending(caller)]

    async def schedule(verified: Verified) -> Response:
        envelope = verified.envelope
        wake = await self_wakes.schedule(
            envelope.principal, _target_id(envelope.target, "thread"), envelope.payload
        )
        return JSONResponse(self_wake_json(wake), status_code=201)

    async def cancel(verified: Verified) -> Response:
        envelope = verified.envelope
        wake = await self_wakes.cancel(envelope.principal, _target_id(envelope.target, "self_wake"))
        return JSONResponse(self_wake_json(wake) | {"cancelled": True})

    app.include_router(router)
    app.exception_handler(SelfWakeRefused)(_refused)
    app.state.actions.register("self_wake.schedule", schedule)
    app.state.actions.register("self_wake.cancel", cancel)


def self_wake_json(wake: SelfWake) -> dict[str, Any]:
    return {
        "id": wake.id,
        "principal": wake.principal,
        "thread_id": wake.thread_id,
        "due_at": rfc3339(wake.due_at),
        "note": wake.note,
        "created_at": rfc3339(wake.created_at),
    }


def _due_at(value: object) -> float:
    if not isinstance(value, str) or len(value) > 40:
        raise SelfWakeRefused("bad_payload", "at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SelfWakeRefused("bad_payload", "at") from None
    if parsed.tzinfo is None:
        raise SelfWakeRefused("bad_payload", "at")
    return parsed.timestamp()


def _note(value: object) -> str:
    if not isinstance(value, str):
        raise SelfWakeRefused("bad_payload", "note")
    text = value.strip()
    if not text or len(text) > NOTE_MAX or not text.isprintable():
        raise SelfWakeRefused("bad_payload", "note")
    return text


def _target_id(target: str, expected: str) -> int:
    scope, _, key = target.partition(":")
    if scope != expected or _ID.fullmatch(key) is None:
        raise SelfWakeRefused("bad_target")
    return int(key)


def _self_wake(row: sqlite3.Row) -> SelfWake:
    return SelfWake(
        row["id"], row["principal"], row["thread_id"], row["due_at"], row["note"], row["created_at"]
    )


async def _refused(request: Request, refused: SelfWakeRefused) -> JSONResponse:
    body = (
        {"reason": refused.reason}
        if refused.field is None
        else {"reason": refused.reason, "field": refused.field}
    )
    return JSONResponse(body, status_code=refused.status)
