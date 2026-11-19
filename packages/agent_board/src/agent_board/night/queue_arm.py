import math
import re
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_board.night import effective_queue as eq
from agent_board.night.effective_queue import MAX_INT, ExpectedControl
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night_state import StateError, check_private_file

INVOCATION = re.compile(r"[0-9a-f]{32}")
_FIELDS = ("run_id", "guard_boot_id", "guard_invocation_id", "deadline_boottime", "deadline_utc")


class Refusal(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    SIDECAR_PRESENT = "SIDECAR_PRESENT"
    BUSY = "BUSY"
    SCHEMA_REFUSED = "SCHEMA_REFUSED"
    ROW_REFUSED = "ROW_REFUSED"
    LEDGER_IDENTITY_MISMATCH = "LEDGER_IDENTITY_MISMATCH"
    CONTROL_MISMATCH = "CONTROL_MISMATCH"
    QUEUE_NOT_EMPTY = "QUEUE_NOT_EMPTY"
    ARM_CONFLICT = "ARM_CONFLICT"
    EXHAUSTED = "EXHAUSTED"


Fault = eq.Fault


@dataclass(frozen=True, slots=True)
class ArmTuple:
    run_id: str
    guard_boot_id: str
    guard_invocation_id: str
    deadline_boottime: float
    deadline_utc: float


@dataclass(frozen=True, slots=True)
class ArmPublication:
    desired: ArmTuple
    expected_prior: ArmTuple | None


@dataclass(frozen=True, slots=True)
class QueueArmResult:
    mutation: MutationOutcome
    cleanup: CleanupOutcome
    changed: bool | None
    control: ExpectedControl | None
    stored: ArmTuple | None
    refusal: Refusal | None
    fault: eq.Fault | None


class _Refused(ValueError):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class _Attempt(eq._Attempt):
    arm_refusal: Refusal | None = None
    stored: ArmTuple | None = field(default=None)

    def outcome(self) -> QueueArmResult:
        usable = (
            self.finished
            and self.cleanup == CleanupOutcome.CLOSED
            and self.arm_refusal is None
            and self.fault is None
            and self.mutation in (MutationOutcome.COMMITTED, MutationOutcome.ROLLED_BACK)
        )
        changed = self.changed if self.mutation == MutationOutcome.COMMITTED else False
        return QueueArmResult(
            self.mutation,
            self.cleanup,
            None if self.mutation == MutationOutcome.UNKNOWN else changed,
            self.control if usable else None,
            self.stored if usable else None,
            self.arm_refusal,
            self.fault,
        )


def _require(condition: bool, refusal: Refusal = Refusal.ROW_REFUSED) -> None:
    if not condition:
        raise _Refused(refusal)


def _tuple(value: object, refusal: Refusal) -> ArmTuple:
    _require(type(value) is ArmTuple, refusal)
    assert isinstance(value, ArmTuple)
    run_id, boot_id, invocation = value.run_id, value.guard_boot_id, value.guard_invocation_id
    boottime, utc = value.deadline_boottime, value.deadline_utc
    try:
        run_id, boot_id = eq._uuid(run_id), eq._uuid(boot_id)
    except eq._Refused:
        raise _Refused(refusal) from None
    _require(type(invocation) is str and INVOCATION.fullmatch(invocation) is not None, refusal)
    for deadline in (boottime, utc):
        _require(type(deadline) is float and math.isfinite(deadline) and deadline >= 0, refusal)
    return ArmTuple(run_id, boot_id, invocation, 0.0 if boottime == 0 else boottime, 0.0 if utc == 0 else utc)


def _publication(value: object) -> ArmPublication:
    bad = Refusal.INVALID_INPUT
    _require(type(value) is ArmPublication, bad)
    assert isinstance(value, ArmPublication)
    desired, prior = value.desired, value.expected_prior
    checked = ArmPublication(_tuple(desired, bad), None if prior is None else _tuple(prior, bad))
    _require(checked.expected_prior is None or checked.expected_prior.run_id == checked.desired.run_id, bad)
    return checked


_ARM = {
    "run_id": ("text", 36),
    "guard_boot_id": ("text", 36),
    "guard_invocation_id": ("text", 32),
    "deadline_boottime": ("real", 0),
    "deadline_utc": ("real", 0),
}


def _projection() -> str:
    columns: list[str] = []
    for name, (kind, cap) in _ARM.items():
        value = (
            f"CASE WHEN typeof({name})='real' THEN {name} END"
            if kind == "real"
            else f"substr(CAST({name} AS BLOB),1,{cap + 1})"
        )
        columns.extend((f"{value} AS {name}", f"typeof({name}) AS {name}_kind"))
    return ",".join(columns)


ARM_QUERY = f"SELECT {_projection()} FROM arms INDEXED BY sqlite_autoindex_arms_1 WHERE run_id=? LIMIT 2"
INSERT_ARM = "INSERT INTO arms(run_id,guard_boot_id,guard_invocation_id,deadline_boottime,deadline_utc) VALUES(?,?,?,?,?)"
REPLACE_ARM = (
    "UPDATE arms SET guard_boot_id=?,guard_invocation_id=?,deadline_boottime=?,deadline_utc=? "
    "WHERE run_id=? AND guard_boot_id=? AND guard_invocation_id=? AND deadline_boottime=? AND deadline_utc=?"
)
ADVANCE = "UPDATE effective_queue SET revision=? WHERE singleton=1 AND generation=? AND revision=? AND active_seq IS NULL"


def _current(conn: sqlite3.Connection, run_id: str) -> ArmTuple | None:
    rows = conn.execute(ARM_QUERY, (run_id,)).fetchall()
    _require(len(rows) <= 1)
    if not rows:
        return None
    values: dict[str, Any] = {}
    for name, (kind, cap) in _ARM.items():
        value, actual = rows[0][name], rows[0][f"{name}_kind"]
        _require(actual == kind)
        if kind == "real":
            _require(type(value) is float)
            values[name] = value
        else:
            _require(type(value) is bytes and len(value) <= cap)
            try:
                values[name] = value.decode("utf-8")
            except UnicodeError:
                raise _Refused(Refusal.ROW_REFUSED) from None
    stored = _tuple(ArmTuple(**values), Refusal.ROW_REFUSED)
    _require(stored.run_id == run_id)
    return stored


def _write(conn: sqlite3.Connection, statement: str, parameters: tuple[object, ...]) -> None:
    if conn.execute(statement, parameters).rowcount != 1:
        raise eq._Failed(Fault.SNAPSHOT_FAILURE)


def _values(arm: ArmTuple) -> tuple[object, ...]:
    return tuple(getattr(arm, name) for name in _FIELDS)


def _apply(
    conn: sqlite3.Connection, attempt: _Attempt, expected: ExpectedControl, publication: ArmPublication
) -> None:
    _require(expected.active_seq is None, Refusal.QUEUE_NOT_EMPTY)
    desired, prior = publication.desired, publication.expected_prior
    current = _current(conn, desired.run_id)
    _require(current == prior, Refusal.ARM_CONFLICT)
    if current == desired:
        attempt.control, attempt.stored = expected, desired
        eq._rollback(conn, attempt)
        return
    _require(expected.revision < MAX_INT, Refusal.EXHAUSTED)
    attempt.changed = True
    if prior is None:
        _write(conn, INSERT_ARM, _values(desired))
    else:
        _write(conn, REPLACE_ARM, (*_values(desired)[1:], *_values(prior)))
    _write(conn, ADVANCE, (expected.revision + 1, expected.generation, expected.revision))
    after = ExpectedControl(
        expected.journal_id, expected.generation, expected.revision + 1, None, expected.acked_counter
    )
    try:
        stored = _current(conn, desired.run_id)
        control = eq._read_control(conn)
    except (_Refused, eq._Refused):
        raise eq._Failed(Fault.SNAPSHOT_FAILURE) from None
    if stored != desired or control != after:
        raise eq._Failed(Fault.SNAPSHOT_FAILURE)
    attempt.control, attempt.stored = after, desired
    attempt.commit_attempted = True
    conn.execute("COMMIT")
    attempt.mutation = MutationOutcome.COMMITTED


def compare_and_set_queue_arm(
    path: Path, *, expected: ExpectedControl, publication: ArmPublication
) -> QueueArmResult:
    attempt = _Attempt()
    conn: sqlite3.Connection | None = None
    try:
        try:
            checked_path, control = eq._path(path), eq._control(expected)
            checked = _publication(publication)
        except AttributeError:
            raise _Refused(Refusal.INVALID_INPUT) from None
        except eq._Refused:
            raise _Refused(Refusal.INVALID_INPUT) from None
        identity = eq._existing(checked_path, attempt)
        conn = sqlite3.connect(f"{checked_path.as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=0)
        conn.row_factory = sqlite3.Row
        check_private_file(checked_path)
        metadata = checked_path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity:
            raise StateError("ledger_replaced")
        for statement in ("PRAGMA busy_timeout=0", "PRAGMA trusted_schema=OFF", "PRAGMA synchronous=EXTRA"):
            conn.execute(statement)
        conn.execute("BEGIN IMMEDIATE")
        attempt.begun = True
        attempt.mutation = MutationOutcome.UNKNOWN
        eq._validate(conn)
        eq._check_control(conn, control)
        _apply(conn, attempt, control, checked)
        attempt.finished = True
    except _Refused as error:
        attempt.arm_refusal = error.reason
    except eq._Refused as error:
        attempt.arm_refusal = Refusal(error.reason.value)
    except eq._Failed as error:
        attempt.failed(error.reason)
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if (
            not attempt.begun
            and type(code) is int
            and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        ):
            attempt.arm_refusal = Refusal.BUSY
        else:
            attempt.failed(Fault.SQLITE_FAILURE)
    except (StateError, OSError, ValueError, TypeError, OverflowError):
        attempt.failed(Fault.FILE_FAILURE)
    finally:
        if conn is not None:
            eq._finish(conn, attempt)
    return attempt.outcome()
