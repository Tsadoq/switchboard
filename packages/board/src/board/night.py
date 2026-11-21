import hashlib
import math
import re
import secrets
import sqlite3
import time as clock
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from functools import partial
from uuid import uuid4
from zoneinfo import ZoneInfo

from board.db import Database

ROME = ZoneInfo("Europe/Rome")
BUDGET_SECONDS = 1200
FLEET_CAPACITY = 2
INITIAL_REASON = "night_initial"
INITIAL_SUMMARY = "the nightly start"


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
    elapsed: float | None
    state: str


class NightAdmission:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def enroll(self, principal: str, enabled: bool, *, now: float | None = None) -> None:
        _box(principal)
        timestamp = clock.time() if now is None else now

        def write(conn: sqlite3.Connection) -> None:
            hold_wakes_in_transaction(conn, timestamp, principal)
            conn.execute(
                "INSERT INTO night_enrollment (principal, enabled) VALUES (?, ?) "
                "ON CONFLICT(principal) DO UPDATE SET enabled = excluded.enabled",
                (principal, enabled),
            )
            hold_wakes_in_transaction(conn, timestamp, principal)
            if not enabled:
                conn.execute(
                    "UPDATE night_runs SET state = 'stopping' WHERE principal = ? AND state IN ('admitted', 'running')",
                    (principal,),
                )

        await self._db.transact(write)

    async def set_enabled(self, enabled: bool, *, now: float | None = None) -> None:
        timestamp = clock.time() if now is None else now

        def write(conn: sqlite3.Connection) -> None:
            hold_wakes_in_transaction(conn, timestamp)
            conn.execute("UPDATE night_control SET enabled = ? WHERE singleton = 1", (enabled,))
            if not enabled:
                conn.execute(
                    "UPDATE night_runs SET state = 'stopping' WHERE state IN ('admitted', 'running')"
                )

        await self._db.transact(write)

    async def admit(self, principal: str, now: float) -> Run:
        return await self._db.transact(lambda conn: admit_in_transaction(conn, principal, now))

    async def start(self, principal: str, run_id: str, fence: str, now: float) -> Run:
        return await self._db.transact(lambda conn: start_in_transaction(conn, principal, run_id, fence, now))

    async def settle(self, principal: str, run_id: str, fence: str, *, elapsed: float, stopped: bool) -> Run:
        return await self._db.transact(
            lambda conn: settle_in_transaction(
                conn, principal, run_id, fence, elapsed=elapsed, stopped=stopped
            )
        )

    async def run(self, run_id: str) -> Run:
        return await self._db.read(lambda conn: _find(conn, run_id))

    async def claim_initial(
        self,
        principal: str,
        now: float,
        *,
        emit: Callable[[sqlite3.Connection, NightWindow], None] | None = None,
    ) -> bool:
        return await self._db.transact(
            lambda conn: claim_initial_in_transaction(conn, principal, now, emit=emit)
        )


def claim_initial_in_transaction(
    conn: sqlite3.Connection,
    principal: str,
    now: float,
    *,
    emit: Callable[[sqlite3.Connection, NightWindow], None] | None = None,
) -> bool:
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


def initial_wake_in_transaction(
    conn: sqlite3.Connection, night: NightWindow, *, principal: str, now: float
) -> None:
    """The board's own start for one principal's night, held like any budgeted wake for the night run."""
    conn.execute(
        "INSERT INTO wakes (principal, reason, source_ref, summary, created_at, origin_kind) "
        "VALUES (?, ?, ?, ?, ?, 'budgeted')",
        (principal, INITIAL_REASON, f"night:{night.local_date}", INITIAL_SUMMARY, now),
    )


def admit_in_transaction(conn: sqlite3.Connection, principal: str, now: float) -> Run:
    box = _box(principal)
    _eligible(conn, principal)
    night = window_for(now)
    if night is None:
        raise AdmissionDenied("outside_window")
    if conn.execute(
        "SELECT 1 FROM wakes WHERE principal = ? AND acked_at IS NULL AND origin_kind = 'human' LIMIT 1",
        (principal,),
    ).fetchone():
        raise AdmissionDenied("human_priority")
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


def admitted_for_box_in_transaction(conn: sqlite3.Connection, box: str, now: float) -> list[Run]:
    """Admits each enrolled principal of `box` that has held night work, and lists the box's admitted runs."""
    night = window_for(now)
    if night is None:
        return []
    runs: list[Run] = []
    enrolled = conn.execute("SELECT principal FROM night_enrollment WHERE enabled = 1 ORDER BY principal")
    for principal in [row[0] for row in enrolled]:
        try:
            if _box(principal) != box.casefold():
                continue
            if not _open_run(conn, principal) and _budget_left(conn, principal, night.local_date) <= 0:
                hold_wakes_in_transaction(conn, now, principal)
                continue
            claim_initial_in_transaction(
                conn, principal, now, emit=partial(initial_wake_in_transaction, principal=principal, now=now)
            )
            hold_wakes_in_transaction(conn, now, principal)
            if not _open_run(conn, principal) and not _held_work(conn, principal, night):
                continue
            run = admit_in_transaction(conn, principal, now)
        except AdmissionDenied:
            continue
        if run.state == "admitted":
            runs.append(run)
    return runs


def _open_run(conn: sqlite3.Connection, principal: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM night_runs WHERE principal = ? AND state != 'finished' LIMIT 1", (principal,)
        ).fetchone()
        is not None
    )


def _held_work(conn: sqlite3.Connection, principal: str, night: NightWindow) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM wakes w JOIN night_held_wakes h ON h.wake_id = w.id "
            "WHERE w.principal = ? AND w.acked_at IS NULL AND w.origin_kind = 'budgeted' "
            "AND h.local_date = ? AND NOT EXISTS (SELECT 1 FROM night_captured_wakes cw "
            "JOIN night_wake_captures c ON c.receipt_id = cw.capture_id "
            "JOIN night_runs r ON r.id = c.run_id WHERE cw.wake_id = w.id AND r.state != 'finished') LIMIT 1",
            (principal, night.local_date),
        ).fetchone()
        is not None
    )


def hold_wakes_in_transaction(conn: sqlite3.Connection, now: float, principal: str | None = None) -> None:
    """Holds an enrolled principal's budgeted wakes for its night, after giving back what ended nights still hold.

    Only night control on and enrollment can open a principal's routing day. Once open, that day keeps holding
    its principal's budgeted wakes until its window ends, whatever control or enrollment do in the meantime.
    """
    release_held_wakes_in_transaction(conn, now, principal)
    night = window_for(now)
    if night is None:
        return
    if _control_on(conn):
        conn.execute(
            "INSERT INTO night_routing_days (principal, local_date, starts_at, ends_at) "
            "SELECT principal, ?, ?, ? FROM night_enrollment "
            "WHERE enabled = 1 AND (? IS NULL OR principal = ?) "
            "ON CONFLICT (principal, local_date) DO NOTHING",
            (night.local_date, night.start, night.end, principal, principal),
        )
    conn.execute(
        "INSERT INTO night_held_wakes (wake_id, local_date, held_at) "
        "SELECT w.id, ?, ? FROM wakes w JOIN night_routing_days d ON d.principal = w.principal "
        "AND d.local_date = ? WHERE w.acked_at IS NULL AND w.origin_kind = 'budgeted' "
        "AND (? IS NULL OR w.principal = ?) ON CONFLICT (wake_id) DO NOTHING",
        (night.local_date, now, night.local_date, principal, principal),
    )


def release_held_wakes_in_transaction(
    conn: sqlite3.Connection, now: float, principal: str | None = None
) -> None:
    """Gives back never-captured wakes once their night has ended and no unfinished run could still own them.

    A never-captured ordinary wake becomes day work again. The board's own never-captured night_initial start is
    retired by acknowledging it. A captured wake is never released or acknowledged here, even when its run
    finished: a finished run is not proof the wake was handled, so captured wakes stay held as recorded debt.
    """
    rows = conn.execute(
        "SELECT h.wake_id, w.reason FROM night_held_wakes h JOIN wakes w ON w.id = h.wake_id "
        "JOIN night_routing_days d ON d.principal = w.principal AND d.local_date = h.local_date "
        "WHERE d.ends_at <= ? AND (? IS NULL OR w.principal = ?) "
        "AND NOT EXISTS (SELECT 1 FROM night_captured_wakes cw WHERE cw.wake_id = h.wake_id) "
        "AND NOT EXISTS (SELECT 1 FROM night_runs r WHERE r.principal = w.principal AND r.state != 'finished')",
        (now, principal, principal),
    ).fetchall()
    retired = [(now, row[0]) for row in rows if row[1] == INITIAL_REASON]
    conn.executemany("UPDATE wakes SET acked_at = ? WHERE id = ? AND acked_at IS NULL", retired)
    conn.executemany("DELETE FROM night_held_wakes WHERE wake_id = ?", [(row[0],) for row in rows])


def _control_on(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT enabled FROM night_control WHERE singleton = 1").fetchone()[0])


def _budget_left(conn: sqlite3.Connection, principal: str, local_date: str) -> float:
    row = conn.execute(
        "SELECT spent + reserved FROM night_budget WHERE principal = ? AND local_date = ?",
        (principal, local_date),
    ).fetchone()
    return BUDGET_SECONDS - (row[0] if row is not None else 0)


def start_in_transaction(
    conn: sqlite3.Connection, principal: str, run_id: str, fence: str, now: float
) -> Run:
    _eligible(conn, principal)
    run = _fenced(conn, principal, run_id, fence)
    night = window_for(now)
    if night is None or now >= run.deadline or now < run.admitted_at:
        raise AdmissionDenied("expired")
    if night.local_date != run.local_date or run.state not in ("admitted", "running"):
        raise AdmissionDenied("stopped")
    if run.state == "admitted":
        conn.execute("UPDATE night_runs SET state = 'running', started_at = ? WHERE id = ?", (now, run_id))
    return _find(conn, run_id)


def settle_in_transaction(
    conn: sqlite3.Connection, principal: str, run_id: str, fence: str, *, elapsed: float, stopped: bool
) -> Run:
    run = _fenced(conn, principal, run_id, fence)
    if not math.isfinite(elapsed) or elapsed < 0:
        raise AdmissionDenied("invalid_elapsed")
    if run.state == "finished":
        return run
    measured = max(elapsed, run.elapsed or 0)
    overran = measured > run.reserved
    charged = conn.execute("SELECT charged FROM night_runs WHERE id = ?", (run_id,)).fetchone()[0]
    if charged is None:
        charged = measured if stopped and not overran else run.reserved
        conn.execute(
            "UPDATE night_budget SET reserved = reserved - ?, spent = spent + ? WHERE principal = ? AND local_date = ?",
            (run.reserved, charged, principal, run.local_date),
        )
    state = "finished" if stopped and not overran else "quarantined"
    conn.execute(
        "UPDATE night_runs SET charged = ?, elapsed = ?, state = ? WHERE id = ?",
        (charged, measured, state, run_id),
    )
    return _find(conn, run_id)


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
        row["elapsed"],
        row["state"],
    )
