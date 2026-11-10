import hashlib
import math
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

from board.db import Database

ROME = ZoneInfo("Europe/Rome")
BUDGET_SECONDS = 1200
FLEET_CAPACITY = 2


class AdmissionDenied(ValueError):
    pass


@dataclass(frozen=True)
class NightWindow:
    local_date: str
    start: float
    end: float

    def slot(self, principal: str) -> float:
        digest = hashlib.sha256(f"night-v1:{self.local_date}:{principal}".encode()).digest()
        return self.start + int.from_bytes(digest, "big") % int(self.end - self.start)


def window_on(day: date) -> NightWindow:
    start = datetime.combine(day, time(1), ROME).timestamp()
    end = datetime.combine(day, time(6), ROME).timestamp()
    return NightWindow(day.isoformat(), start, end)


def window_for(now: float) -> NightWindow | None:
    if not math.isfinite(now):
        raise AdmissionDenied("invalid_time")
    try:
        local = datetime.fromtimestamp(now, UTC).astimezone(ROME)
    except (OverflowError, ValueError, OSError):
        raise AdmissionDenied("invalid_time") from None
    night = window_on(local.date())
    return night if night.start <= now < night.end else None


@dataclass(frozen=True)
class Run:
    id: str
    principal: str
    box: str
    local_date: str
    fence: str
    admitted_at: float
    started_at: float | None
    deadline: float
    reserved: float
    state: str


class NightAdmission:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def enroll(self, principal: str, enabled: bool) -> None:
        _box(principal)

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO night_enrollment (principal, enabled) VALUES (?, ?) "
                "ON CONFLICT(principal) DO UPDATE SET enabled = excluded.enabled",
                (principal, enabled),
            )
            if not enabled:
                conn.execute(
                    "UPDATE night_runs SET state = 'stopping' WHERE principal = ? AND state IN ('admitted', 'running')",
                    (principal,),
                )

        await self._db.transact(write)

    async def set_enabled(self, enabled: bool) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE night_control SET enabled = ? WHERE singleton = 1", (enabled,))
            if not enabled:
                conn.execute(
                    "UPDATE night_runs SET state = 'stopping' WHERE state IN ('admitted', 'running')"
                )

        await self._db.transact(write)

    async def admit(self, principal: str, now: float) -> Run:
        box = _box(principal)

        def write(conn: sqlite3.Connection) -> Run:
            _eligible(conn, principal)
            night = window_for(now)
            if night is None:
                raise AdmissionDenied("outside_window")
            existing = conn.execute(
                "SELECT * FROM night_runs WHERE principal = ? AND state != 'finished'", (principal,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["state"] in ("admitted", "running")
                    and existing["deadline"] > now
                    and existing["local_date"] == night.local_date
                ):
                    return _run(existing)
                raise AdmissionDenied("capacity")
            occupied = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(box = ?), 0) FROM night_runs WHERE state != 'finished'", (box,)
            ).fetchone()
            if occupied[0] >= FLEET_CAPACITY or occupied[1]:
                raise AdmissionDenied("capacity")
            conn.execute(
                "INSERT INTO night_budget (principal, local_date, starts_at, ends_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(principal, local_date) DO NOTHING",
                (principal, night.local_date, night.start, night.end),
            )
            budget = conn.execute(
                "SELECT spent, reserved FROM night_budget WHERE principal = ? AND local_date = ?",
                (principal, night.local_date),
            ).fetchone()
            reserved = min(BUDGET_SECONDS - budget[0] - budget[1], night.end - now)
            if reserved <= 0:
                raise AdmissionDenied("budget")
            run_id, fence = str(uuid4()), secrets.token_urlsafe(32)
            conn.execute(
                "UPDATE night_budget SET reserved = reserved + ? WHERE principal = ? AND local_date = ?",
                (reserved, principal, night.local_date),
            )
            conn.execute(
                "INSERT INTO night_runs (id, principal, box, local_date, fence, admitted_at, deadline, reserved, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'admitted')",
                (run_id, principal, box, night.local_date, fence, now, now + reserved, reserved),
            )
            return _find(conn, run_id)

        return await self._db.transact(write)

    async def start(self, principal: str, run_id: str, fence: str, now: float) -> Run:
        def write(conn: sqlite3.Connection) -> Run:
            _eligible(conn, principal)
            run = _fenced(conn, principal, run_id, fence)
            night = window_for(now)
            if night is None or now >= run.deadline or now < run.admitted_at:
                raise AdmissionDenied("expired")
            if night.local_date != run.local_date or run.state not in ("admitted", "running"):
                raise AdmissionDenied("stopped")
            if run.state == "admitted":
                conn.execute(
                    "UPDATE night_runs SET state = 'running', started_at = ? WHERE id = ?", (now, run_id)
                )
            return _find(conn, run_id)

        return await self._db.transact(write)

    async def settle(self, principal: str, run_id: str, fence: str, *, elapsed: float, stopped: bool) -> Run:
        def write(conn: sqlite3.Connection) -> Run:
            run = _fenced(conn, principal, run_id, fence)
            if not math.isfinite(elapsed) or not 0 <= elapsed <= run.reserved:
                raise AdmissionDenied("invalid_elapsed")
            if run.state == "finished":
                return run
            charged = conn.execute("SELECT charged FROM night_runs WHERE id = ?", (run_id,)).fetchone()[0]
            if charged is None:
                charged = elapsed if stopped else run.reserved
                conn.execute(
                    "UPDATE night_budget SET reserved = reserved - ?, spent = spent + ? WHERE principal = ? AND local_date = ?",
                    (run.reserved, charged, principal, run.local_date),
                )
            state = "finished" if stopped else "quarantined"
            conn.execute(
                "UPDATE night_runs SET charged = ?, state = ? WHERE id = ?", (charged, state, run_id)
            )
            return _find(conn, run_id)

        return await self._db.transact(write)

    async def run(self, run_id: str) -> Run:
        return await self._db.read(lambda conn: _find(conn, run_id))

    async def claim_initial(
        self,
        principal: str,
        now: float,
        *,
        emit: Callable[[sqlite3.Connection, NightWindow], None] | None = None,
    ) -> bool:
        def write(conn: sqlite3.Connection) -> bool:
            try:
                _eligible(conn, principal)
            except AdmissionDenied:
                return False
            night = window_for(now)
            if night is None:
                return False
            slot = night.slot(principal)
            if now < slot:
                return False
            status = "fired" if now < slot + 60 else "skipped"
            cursor = conn.execute(
                "INSERT INTO night_initial_slots (principal, local_date, slot_at, status) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(principal, local_date) DO NOTHING",
                (principal, night.local_date, slot, status),
            )
            fired = cursor.rowcount == 1 and status == "fired"
            if fired and emit is not None:
                emit(conn, night)
            return fired

        return await self._db.transact(write)


def _box(principal: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+", principal) is None:
        raise AdmissionDenied("excluded")
    box = principal.partition("@")[2]
    if box.casefold() in ("ops", "heimdall"):
        raise AdmissionDenied("excluded")
    return box.casefold()


def _eligible(conn: sqlite3.Connection, principal: str) -> None:
    _box(principal)
    if not conn.execute("SELECT enabled FROM night_control WHERE singleton = 1").fetchone()[0]:
        raise AdmissionDenied("disabled")
    row = conn.execute("SELECT enabled FROM night_enrollment WHERE principal = ?", (principal,)).fetchone()
    if row is None or not row[0]:
        raise AdmissionDenied("not_enrolled")


def _find(conn: sqlite3.Connection, run_id: str) -> Run:
    row = conn.execute("SELECT * FROM night_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise AdmissionDenied("stale_fence")
    return _run(row)


def _fenced(conn: sqlite3.Connection, principal: str, run_id: str, fence: str) -> Run:
    run = _find(conn, run_id)
    if run.principal != principal or not secrets.compare_digest(run.fence, fence):
        raise AdmissionDenied("stale_fence")
    return run


def _run(row: sqlite3.Row) -> Run:
    return Run(
        row["id"],
        row["principal"],
        row["box"],
        row["local_date"],
        row["fence"],
        row["admitted_at"],
        row["started_at"],
        row["deadline"],
        row["reserved"],
        row["state"],
    )
