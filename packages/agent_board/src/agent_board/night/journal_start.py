import math
import re
import sqlite3
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq
from agent_board.night.effective_queue import MAX_INT
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.queue_association import (
    _JOURNAL_CATALOG,
    _RUN,
    ACTIVE_QUERY,
    JOURNAL_CATALOG_QUERY,
    JOURNAL_VERSION,
    RUN_QUERY,
)
from agent_board.night.queue_association import FENCE as LEGACY_FENCE
from agent_board.night_state import (
    MAX_RUN_SECONDS,
    MIN_START_SECONDS,
    ClockReading,
    Grant,
    StateError,
    check_private_file,
)

FENCE = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_PRINCIPALS = 64
STATES = ("reserved", "starting", "stopping", "quarantined", "stopped")
SETUP = ("PRAGMA busy_timeout=0", "PRAGMA synchronous=EXTRA", "PRAGMA trusted_schema=OFF", "BEGIN IMMEDIATE")
INSERT = (
    "INSERT INTO runs (run_id, fence, principal, uid, boot_id, deadline_boottime, deadline_utc, "
    "state, launch_claimed, stop_requested, uncertain, reason, slot) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, 'starting', 1, 0, 0, NULL, 1)"
)


class Refusal(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    PRINCIPAL_BINDING = "PRINCIPAL_BINDING"
    BOOT_MISMATCH = "BOOT_MISMATCH"
    GRANT_WINDOW_REFUSED = "GRANT_WINDOW_REFUSED"
    PATH_REFUSED = "PATH_REFUSED"
    SIDECAR_PRESENT = "SIDECAR_PRESENT"
    BUSY = "BUSY"
    SCHEMA_REFUSED = "SCHEMA_REFUSED"
    ROW_REFUSED = "ROW_REFUSED"
    RUN_ALREADY_RECORDED = "RUN_ALREADY_RECORDED"
    ACTIVE_PRESENT = "ACTIVE_PRESENT"


class Fault(StrEnum):
    FILE_FAILURE = "FILE_FAILURE"
    SQLITE_FAILURE = "SQLITE_FAILURE"
    SNAPSHOT_FAILURE = "SNAPSHOT_FAILURE"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"
    CLOSE_FAILURE = "CLOSE_FAILURE"


_CLEANUP_FAULTS = (Fault.ROLLBACK_FAILURE, Fault.CLOSE_FAILURE)
_TRIGGERS = {
    MutationOutcome.NOT_ATTEMPTED: (Fault.FILE_FAILURE, Fault.SQLITE_FAILURE),
    MutationOutcome.ROLLED_BACK: (Fault.SQLITE_FAILURE, Fault.SNAPSHOT_FAILURE),
    MutationOutcome.UNKNOWN: (Fault.SQLITE_FAILURE,),
    MutationOutcome.COMMITTED: (),
}


def _consistent(
    mutation: MutationOutcome, cleanup: CleanupOutcome, refusal: Refusal | None, fault: Fault | None
) -> bool:
    if refusal is not None and mutation is not MutationOutcome.NOT_ATTEMPTED:
        return False
    if cleanup is CleanupOutcome.UNCERTAIN:
        if mutation is MutationOutcome.COMMITTED or mutation is MutationOutcome.ROLLED_BACK:
            return fault is Fault.CLOSE_FAILURE
        return fault in _CLEANUP_FAULTS
    if refusal is not None:
        return fault is None
    if fault is None:
        return mutation is MutationOutcome.COMMITTED
    return fault in _TRIGGERS[mutation]


@dataclass(frozen=True, slots=True)
class StartClaimResult:
    mutation: MutationOutcome
    cleanup: CleanupOutcome
    refusal: Refusal | None
    fault: Fault | None

    def __post_init__(self) -> None:
        mutation, cleanup, refusal, fault = self.mutation, self.cleanup, self.refusal, self.fault
        if (
            type(mutation) is not MutationOutcome
            or type(cleanup) is not CleanupOutcome
            or (refusal is not None and type(refusal) is not Refusal)
            or (fault is not None and type(fault) is not Fault)
        ):
            raise TypeError("invalid start claim result")
        if not _consistent(mutation, cleanup, refusal, fault):
            raise ValueError("inconsistent start claim result")

    @property
    def usable(self) -> bool:
        return (
            self.mutation is MutationOutcome.COMMITTED
            and self.cleanup is CleanupOutcome.CLOSED
            and self.refusal is None
            and self.fault is None
        )


class _Refused(ValueError):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


class _Faulted(RuntimeError):
    def __init__(self, reason: Fault) -> None:
        self.reason = reason
        super().__init__(reason)


def _require(condition: bool, refusal: Refusal = Refusal.INVALID_INPUT) -> None:
    if not condition:
        raise _Refused(refusal)


@dataclass(frozen=True, slots=True)
class _Inputs:
    path: Path
    row: tuple[str, str, str, int, str, float, float]
    principals: dict[str, int]
    clock: tuple[str, float, float]
    grace: float


@dataclass
class _State:
    conn: sqlite3.Connection | None = None
    begun: bool = False
    dml_attempted: bool = False
    commit_attempted: bool = False
    mutation: MutationOutcome = MutationOutcome.NOT_ATTEMPTED
    cleanup: CleanupOutcome = CleanupOutcome.CLOSED
    refusal: Refusal | None = None
    fault: Fault | None = None

    def failed(self, fault: Fault) -> None:
        priority = {Fault.CLOSE_FAILURE: 3, Fault.ROLLBACK_FAILURE: 2}
        if self.fault is None or priority.get(fault, 1) > priority.get(self.fault, 1):
            self.fault = fault


def _uuid(value: object) -> str:
    try:
        return eq._uuid(value, eq.Refusal.INVALID_INPUT)
    except eq._Refused:
        raise _Refused(Refusal.INVALID_INPUT) from None


def _float(value: object, *, positive: bool) -> float:
    _require(type(value) is float)
    assert isinstance(value, float)
    _require(math.isfinite(value) and (value > 0 if positive else value >= 0))
    return 0.0 if value == 0 else value


def _principal(value: object) -> str:
    _require(type(value) is str)
    assert isinstance(value, str)
    _require(value.isascii() and len(value) <= cq.PRINCIPAL_MAX and cq.PRINCIPAL.fullmatch(value) is not None)
    return value


def _uid(value: object) -> int:
    _require(type(value) is int)
    assert isinstance(value, int)
    _require(1 <= value <= MAX_INT)
    return value


def _principals(value: object) -> dict[str, int]:
    _require(type(value) is dict)
    assert isinstance(value, dict)
    count = len(value)
    _require(1 <= count <= MAX_PRINCIPALS)
    owned: dict[str, int] = {}
    try:
        for index, (key, uid) in enumerate(value.items()):
            _require(index < MAX_PRINCIPALS)
            owned[_principal(key)] = _uid(uid)
    except RuntimeError:
        raise _Refused(Refusal.INVALID_INPUT) from None
    _require(len(owned) == count == len(value))
    return owned


def _inputs(path: object, expected: object, principals: object, now: object, grace: object) -> _Inputs:
    checked_path = _path(path)
    _require(type(expected) is Grant)
    assert isinstance(expected, Grant)
    run_id, fence, principal, uid, boot_id, boottime, utc = (
        expected.run_id,
        expected.fence,
        expected.principal,
        expected.uid,
        expected.boot_id,
        expected.deadline_boottime,
        expected.deadline_utc,
    )
    _require(type(fence) is str and FENCE.fullmatch(fence) is not None)
    row = (
        _uuid(run_id),
        fence,
        _principal(principal),
        _uid(uid),
        _uuid(boot_id),
        _float(boottime, positive=True),
        _float(utc, positive=True),
    )
    _require(type(now) is ClockReading)
    assert isinstance(now, ClockReading)
    clock_boot, clock_boottime, clock_utc = now.boot_id, now.boottime, now.utc
    clock = (_uuid(clock_boot), _float(clock_boottime, positive=False), _float(clock_utc, positive=False))
    owned = _principals(principals)
    return _Inputs(checked_path, row, owned, clock, _float(grace, positive=True))


def _path(value: object) -> Path:
    try:
        return eq._path(value)
    except eq._Refused:
        raise _Refused(Refusal.INVALID_INPUT) from None


def _admissible(inputs: _Inputs) -> None:
    _, _, principal, uid, boot_id, deadline_boottime, deadline_utc = inputs.row
    _require(inputs.principals.get(principal) == uid, Refusal.PRINCIPAL_BINDING)
    clock_boot, clock_boottime, clock_utc = inputs.clock
    _require(boot_id == clock_boot, Refusal.BOOT_MISMATCH)
    remaining = (deadline_boottime - clock_boottime, deadline_utc - clock_utc)
    _require(
        max(remaining) <= MAX_RUN_SECONDS and min(remaining) - inputs.grace > MIN_START_SECONDS,
        Refusal.GRANT_WINDOW_REFUSED,
    )


def _exact_mode(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except OSError:
        raise _Refused(Refusal.PATH_REFUSED) from None
    _require(mode == 0o600, Refusal.PATH_REFUSED)


def _preflight(path: Path, state: _State) -> tuple[int, int]:
    probe = eq._Attempt()
    try:
        _exact_mode(path)
        return eq._existing(path, probe)
    except eq._Refused as error:
        raise _Refused(Refusal(error.reason.value)) from None
    except (StateError, OSError) as error:
        if probe.cleanup == CleanupOutcome.UNCERTAIN:
            state.cleanup = CleanupOutcome.UNCERTAIN
            raise _Faulted(Fault.CLOSE_FAILURE) from None
        if isinstance(error, OSError):
            raise _Faulted(Fault.FILE_FAILURE) from None
        raise _Refused(Refusal.PATH_REFUSED) from None


def _open(path: Path, identity: tuple[int, int], state: _State) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=0)
    state.conn = conn
    conn.row_factory = sqlite3.Row
    try:
        check_private_file(path)
        metadata = path.lstat()
    except (StateError, OSError):
        raise _Refused(Refusal.PATH_REFUSED) from None
    _require((metadata.st_dev, metadata.st_ino) == identity, Refusal.PATH_REFUSED)
    _require(stat.S_IMODE(metadata.st_mode) == 0o600, Refusal.PATH_REFUSED)
    return conn


def _setup(conn: sqlite3.Connection, state: _State) -> None:
    for statement in SETUP:
        conn.execute(statement)
    state.begun = True


def _catalog(conn: sqlite3.Connection) -> None:
    for statement, expected in (("PRAGMA journal_mode", "delete"), ("PRAGMA user_version", JOURNAL_VERSION)):
        rows = conn.execute(statement).fetchall()
        _require(
            len(rows) == 1 and type(rows[0][0]) is type(expected) and rows[0][0] == expected,
            Refusal.SCHEMA_REFUSED,
        )
    rows = conn.execute(JOURNAL_CATALOG_QUERY).fetchall()
    _require(len(rows) == len(_JOURNAL_CATALOG), Refusal.SCHEMA_REFUSED)
    actual: dict[str, tuple[object, object, object]] = {}
    try:
        for raw in rows:
            row = eq._row(raw, eq._CATALOG, nullable=frozenset(("sql",)))
            name, statement = row["name"], row["sql"]
            _require(isinstance(name, str) and name not in actual, Refusal.SCHEMA_REFUSED)
            actual[name] = (
                row["type"],
                row["tbl_name"],
                eq._sql(statement) if isinstance(statement, str) else None,
            )
    except eq._Refused:
        raise _Refused(Refusal.SCHEMA_REFUSED) from None
    _require(actual == _JOURNAL_CATALOG, Refusal.SCHEMA_REFUSED)


def _stored(raw: Any, run_id: str) -> dict[str, Any]:
    try:
        row = cq._row(raw, _RUN, frozenset(("reason",)))
        _uuid(row["run_id"])
        _uuid(row["boot_id"])
        _principal(row["principal"])
        _uid(row["uid"])
    except (cq._Refused, _Refused):
        raise _Refused(Refusal.ROW_REFUSED) from None
    _require(row["run_id"] == run_id, Refusal.ROW_REFUSED)
    _require(LEGACY_FENCE.fullmatch(row["fence"]) is not None, Refusal.ROW_REFUSED)
    for key in ("deadline_boottime", "deadline_utc"):
        _require(math.isfinite(row[key]) and row[key] >= 0, Refusal.ROW_REFUSED)
    for key in ("launch_claimed", "stop_requested", "uncertain"):
        _require(row[key] in (0, 1), Refusal.ROW_REFUSED)
    _require(row["slot"] == 1 and row["state"] in STATES, Refusal.ROW_REFUSED)
    return row


def _active(rows: list[Any]) -> list[str]:
    _require(len(rows) <= 1, Refusal.ROW_REFUSED)
    found: list[str] = []
    for row in rows:
        value = row["run_id"]
        _require(
            row["run_id_kind"] == "text" and type(value) is bytes and len(value) == 36, Refusal.ROW_REFUSED
        )
        try:
            found.append(_uuid(value.decode("ascii")))
        except (UnicodeError, _Refused):
            raise _Refused(Refusal.ROW_REFUSED) from None
    return found


def _prewrite(conn: sqlite3.Connection, run_id: str) -> None:
    rows = conn.execute(RUN_QUERY, (run_id,)).fetchall()
    _require(len(rows) <= 1, Refusal.ROW_REFUSED)
    if rows:
        _stored(rows[0], run_id)
        raise _Refused(Refusal.RUN_ALREADY_RECORDED)
    _require(not _active(conn.execute(ACTIVE_QUERY).fetchall()), Refusal.ACTIVE_PRESENT)


def _insert(conn: sqlite3.Connection, inputs: _Inputs, state: _State) -> None:
    state.dml_attempted = True
    cursor = conn.execute(INSERT, inputs.row)
    if cursor.rowcount != 1:
        raise _Faulted(Fault.SNAPSHOT_FAILURE)


def _postwrite(conn: sqlite3.Connection, inputs: _Inputs) -> None:
    run_id = inputs.row[0]
    expected = (*inputs.row, "starting", 1, 0, 0, None, 1)
    try:
        rows = conn.execute(RUN_QUERY, (run_id,)).fetchall()
        _require(len(rows) == 1)
        row = _stored(rows[0], run_id)
        _require(tuple(row[name] for name in _RUN) == expected)
        _require(_active(conn.execute(ACTIVE_QUERY).fetchall()) == [run_id])
    except _Refused:
        raise _Faulted(Fault.SNAPSHOT_FAILURE) from None


def _commit(conn: sqlite3.Connection, state: _State) -> None:
    state.commit_attempted = True
    conn.execute("COMMIT")
    state.mutation = MutationOutcome.COMMITTED


def _contended(error: sqlite3.Error, state: _State) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if state.begun or type(code) is not int or code < 0:
        return False
    if code & 0xFF not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return False
    if state.conn is None:
        return True
    try:
        return not state.conn.in_transaction
    except sqlite3.Error:
        return False


def _needs_rollback(state: _State) -> bool:
    if state.mutation is MutationOutcome.COMMITTED or state.conn is None:
        return False
    if state.begun:
        return True
    try:
        return state.conn.in_transaction
    except sqlite3.Error:
        return True


def _finish(state: _State) -> None:
    conn = state.conn
    rolled_back = False
    try:
        if conn is not None and _needs_rollback(state):
            try:
                conn.execute("ROLLBACK")
                rolled_back = True
            except sqlite3.Error:
                state.cleanup = CleanupOutcome.UNCERTAIN
                state.failed(Fault.ROLLBACK_FAILURE)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                state.cleanup = CleanupOutcome.UNCERTAIN
                state.failed(Fault.CLOSE_FAILURE)
        if state.commit_attempted and state.mutation is not MutationOutcome.COMMITTED:
            state.mutation = MutationOutcome.UNKNOWN
        elif state.dml_attempted and state.mutation is not MutationOutcome.COMMITTED:
            state.mutation = MutationOutcome.ROLLED_BACK if rolled_back else MutationOutcome.UNKNOWN


def claim_start(
    journal_path: Path,
    *,
    expected: Grant,
    principals: dict[str, int],
    now: ClockReading,
    grace: float,
) -> StartClaimResult:
    state = _State()
    try:
        try:
            inputs = _inputs(journal_path, expected, principals, now, grace)
        except AttributeError:
            raise _Refused(Refusal.INVALID_INPUT) from None
        _admissible(inputs)
        identity = _preflight(inputs.path, state)
        conn = _open(inputs.path, identity, state)
        _setup(conn, state)
        _catalog(conn)
        _prewrite(conn, inputs.row[0])
        _insert(conn, inputs, state)
        _postwrite(conn, inputs)
        _commit(conn, state)
    except _Refused as error:
        if state.dml_attempted:
            state.failed(Fault.SNAPSHOT_FAILURE)
        else:
            state.refusal = error.reason
    except _Faulted as error:
        state.failed(error.reason)
    except sqlite3.Error as error:
        if _contended(error, state):
            state.refusal = Refusal.BUSY
        else:
            state.failed(Fault.SQLITE_FAILURE)
    except (StateError, OSError, ValueError, TypeError, OverflowError):
        state.failed(Fault.FILE_FAILURE if state.conn is None else Fault.SQLITE_FAILURE)
    finally:
        _finish(state)
    return StartClaimResult(state.mutation, state.cleanup, state.refusal, state.fault)
