import secrets
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_board.night.final_snapshot import _JOURNAL_SCHEMA, _sql
from agent_board.night_state import (
    ClockReading,
    Record,
    StateError,
    _identity,
    check_private_file,
    clock_reading,
)


class MutationOutcome(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class CleanupOutcome(StrEnum):
    CLOSED = "closed"
    UNCERTAIN = "cleanup_uncertain"


@dataclass(frozen=True)
class LatchResult:
    primary: MutationOutcome
    expiry: MutationOutcome
    cleanup: CleanupOutcome
    primary_changed: bool | None
    latch_changed: bool | None
    expiry_changed: bool | None
    record: Record | None
    domain: str | None
    fault: str | None


@dataclass
class _Transaction:
    outcome: MutationOutcome = MutationOutcome.NOT_ATTEMPTED
    begun: bool = False
    body_entered: bool = False
    commit_attempted: bool = False
    rollback_attempted: bool = False
    changed: bool = False

    def enter_body(self) -> None:
        self.body_entered = True
        self.outcome = MutationOutcome.UNKNOWN

    def rolled_back(self) -> None:
        self.begun = False
        if self.body_entered and not self.commit_attempted:
            self.outcome = MutationOutcome.ROLLED_BACK

    def durable_changed(self) -> bool | None:
        if self.outcome == MutationOutcome.COMMITTED:
            return self.changed
        return None if self.outcome == MutationOutcome.UNKNOWN else False


class _InvalidState(StateError):
    pass


class _StaleFence(Exception):
    pass


_TEXT = {"run_id": 36, "fence": 256, "principal": 255, "boot_id": 36, "state": 16, "reason": 256}
_NUMBERS = (
    "uid",
    "deadline_boottime",
    "deadline_utc",
    "launch_claimed",
    "stop_requested",
    "uncertain",
    "slot",
)
_PROJECTION = ", ".join(
    [
        f"substr(CAST({name} AS BLOB),1,{cap + 1}) AS {name},typeof({name}) AS {name}_kind"
        for name, cap in _TEXT.items()
    ]
    + [
        f"CASE WHEN typeof({name}) IN ('integer','real') THEN {name} END AS {name},typeof({name}) AS {name}_kind"
        for name in _NUMBERS
    ]
)
ACTIVE_QUERY = (
    f"SELECT {_PROJECTION} FROM runs INDEXED BY one_active_run WHERE slot=1 AND state!='stopped' LIMIT 2"
)
TARGET_QUERY = f"SELECT {_PROJECTION} FROM runs WHERE run_id=? LIMIT 2"


def latch(journal_path: Path, *, run_id: str, fence: str) -> LatchResult:
    """Propose a local latch within the trusted caller's already-established exclusion interval.

    Current legacy writers do not honor that interval. This API is unhooked, not a stop witness or
    production containment policy. Replacement/maintenance must be excluded until confirmed close.
    """
    primary, expiry = _Transaction(), _Transaction()
    current = primary
    cleanup = CleanupOutcome.CLOSED
    conn: sqlite3.Connection | None = None
    record = None
    latch_changed = False
    domain = None
    fault = None
    try:
        _identity(run_id, fence)
    except (StateError, TypeError, ValueError):
        return LatchResult(
            primary.outcome, expiry.outcome, cleanup, False, None, False, None, None, "invalid_selector"
        )
    try:
        check_private_file(journal_path)
        conn = sqlite3.connect(f"{journal_path.as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("PRAGMA synchronous=EXTRA")
        conn.execute("PRAGMA trusted_schema=OFF")
        _begin(conn, primary)
        primary.enter_body()
        try:
            primary.changed = _refresh(conn)
            rows = conn.execute(TARGET_QUERY, (run_id,)).fetchall()
            if not rows:
                raise _StaleFence
            if len(rows) != 1:
                raise _InvalidState("duplicate_target")
            record = _decode(rows[0])
            if not secrets.compare_digest(record.fence, fence):
                raise _StaleFence
            if not record.stop_requested and record.state in ("reserved", "starting"):
                conn.execute("UPDATE runs SET state='stopping',stop_requested=1 WHERE run_id=?", (run_id,))
                primary.changed = latch_changed = True
                rows = conn.execute(TARGET_QUERY, (run_id,)).fetchall()
                if len(rows) != 1:
                    raise _InvalidState("missing_updated_target")
                record = _decode(rows[0])
        except _StaleFence:
            domain = "stale_fence"
            _rollback(conn, primary)
            current = expiry
            _begin(conn, expiry)
            expiry.enter_body()
            expiry.changed = _refresh(conn)
            _commit(conn, expiry)
        else:
            _commit(conn, primary)
    except sqlite3.Error:
        fault = "sqlite_failure"
    except (StateError, OSError, ValueError, TypeError, OverflowError):
        fault = "validation_or_path_failure"
    finally:
        if conn is not None:
            try:
                if current.begun and not current.rollback_attempted and conn.in_transaction:
                    _rollback(conn, current)
            except sqlite3.Error:
                fault = "rollback_failure"
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    cleanup = CleanupOutcome.UNCERTAIN
                    fault = "close_failure"
                else:
                    if current.begun and not current.commit_attempted:
                        current.rolled_back()
    usable = (
        primary.outcome == MutationOutcome.COMMITTED
        and cleanup == CleanupOutcome.CLOSED
        and fault is None
        and domain is None
    )
    return LatchResult(
        primary.outcome,
        expiry.outcome,
        cleanup,
        primary.durable_changed(),
        latch_changed if primary.outcome == MutationOutcome.COMMITTED else None,
        expiry.durable_changed(),
        record if usable else None,
        domain,
        fault,
    )


def _begin(conn: sqlite3.Connection, transaction: _Transaction) -> None:
    conn.execute("BEGIN IMMEDIATE")
    transaction.begun = True
    _validate(conn)


def _commit(conn: sqlite3.Connection, transaction: _Transaction) -> None:
    transaction.commit_attempted = True
    conn.execute("COMMIT")
    transaction.outcome = MutationOutcome.COMMITTED
    transaction.begun = False


def _rollback(conn: sqlite3.Connection, transaction: _Transaction) -> None:
    transaction.rollback_attempted = True
    conn.execute("ROLLBACK")
    transaction.rolled_back()


def _validate(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise _InvalidState("unsupported_journal_mode")
    if conn.execute("PRAGMA user_version").fetchone()[0] != 2:
        raise _InvalidState("unexpected_schema_version")
    rows = conn.execute(
        "SELECT substr(name,1,129),substr(type,1,17),substr(CAST(sql AS BLOB),1,8193) FROM sqlite_schema LIMIT 33"
    ).fetchall()
    expected = {
        "runs": ("table", _sql(_JOURNAL_SCHEMA[0])),
        "one_active_run": ("index", _sql(_JOURNAL_SCHEMA[1])),
        "sqlite_autoindex_runs_1": ("index", None),
    }
    actual = {}
    if len(rows) > 32:
        raise _InvalidState("oversized_schema")
    for name, kind, sql in rows:
        if (
            not isinstance(name, str)
            or len(name) > 128
            or name in actual
            or (sql is not None and len(sql) > 8192)
        ):
            raise _InvalidState("invalid_schema")
        actual[name] = (kind, None if sql is None else _sql(sql.decode("utf-8")))
    if actual != expected:
        raise _InvalidState("unexpected_schema")


def _refresh(conn: sqlite3.Connection) -> bool:
    now = clock_reading()
    if type(now) is not ClockReading:
        raise _InvalidState("invalid_clock")
    ClockReading(now.boot_id, now.boottime, now.utc)
    rows = conn.execute(ACTIVE_QUERY).fetchall()
    if len(rows) > 1:
        raise _InvalidState("multiple_active_runs")
    if not rows:
        return False
    record = _decode(rows[0])
    if record.state == "quarantined" or (
        record.boot_id == now.boot_id
        and record.deadline_boottime > now.boottime
        and record.deadline_utc > now.utc
    ):
        return False
    never_claimed = not record.launch_claimed and not record.uncertain
    if never_claimed:
        reason = "never_claimed_boot_changed" if record.boot_id != now.boot_id else "never_claimed_expired"
    else:
        reason = "boot_changed" if record.boot_id != now.boot_id else "deadline_passed"
    conn.execute(
        "UPDATE runs SET state=?,stop_requested=1,reason=? WHERE run_id=?",
        ("stopped" if never_claimed else "quarantined", reason, record.run_id),
    )
    return True


def _decode(row: sqlite3.Row) -> Record:
    values: dict[str, Any] = {}
    for name, cap in _TEXT.items():
        value = row[name]
        if name == "reason" and row[f"{name}_kind"] == "null":
            values[name] = None
        elif row[f"{name}_kind"] != "text" or not isinstance(value, bytes) or len(value) > cap:
            raise _InvalidState("invalid_text_storage")
        else:
            values[name] = value.decode("utf-8")
    for name in _NUMBERS:
        if row[f"{name}_kind"] not in ("integer", "real"):
            raise _InvalidState("invalid_numeric_storage")
        values[name] = row[name]
    for name in ("launch_claimed", "stop_requested", "uncertain"):
        if type(values[name]) is not int or values[name] not in (0, 1):
            raise _InvalidState("invalid_flag")
        values[name] = bool(values[name])
    if type(values.pop("slot")) is not int or row["slot"] != 1:
        raise _InvalidState("invalid_slot")
    if values["state"] not in ("reserved", "starting", "stopping", "quarantined", "stopped"):
        raise _InvalidState("invalid_state")
    return Record(**values)
