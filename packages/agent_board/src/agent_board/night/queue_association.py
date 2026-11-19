import math
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq
from agent_board.night import queue_arm as qa
from agent_board.night.assignment import MAX_ASSIGNMENT_BYTES, AssignmentError, NightPolicy
from agent_board.night.effective_queue import MAX_INT, ExpectedControl
from agent_board.night.journal_latch import CleanupOutcome
from agent_board.night.ledger_binding import BindingClaim
from agent_board.night_state import ClockReading, Grant, StateError, check_private_file

JOURNAL_VERSION = 2
FENCE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_JOURNAL_TABLE = (
    "CREATE TABLE IF NOT EXISTS runs ("
    "run_id TEXT PRIMARY KEY, fence TEXT NOT NULL, principal TEXT NOT NULL, uid INTEGER NOT NULL, "
    "boot_id TEXT NOT NULL, deadline_boottime REAL NOT NULL, deadline_utc REAL NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('reserved', 'starting', 'stopping', 'quarantined', 'stopped')), "
    "launch_claimed INTEGER NOT NULL DEFAULT 0 CHECK(launch_claimed IN (0, 1)), "
    "stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0, 1)), "
    "uncertain INTEGER NOT NULL DEFAULT 0 CHECK(uncertain IN (0, 1)), reason TEXT, "
    "slot INTEGER NOT NULL DEFAULT 1 CHECK(slot = 1))"
)
_JOURNAL_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs (slot) WHERE state != 'stopped'"
_JOURNAL_CATALOG = {
    "runs": ("table", "runs", eq._sql(_JOURNAL_TABLE)),
    "sqlite_autoindex_runs_1": ("index", "runs", None),
    "one_active_run": ("index", "runs", eq._sql(_JOURNAL_INDEX)),
}
_RUN = {
    "run_id": ("text", 36),
    "fence": ("text", 128),
    "principal": ("text", cq.PRINCIPAL_MAX),
    "uid": ("integer", 0),
    "boot_id": ("text", 36),
    "deadline_boottime": ("real", 0),
    "deadline_utc": ("real", 0),
    "state": ("text", 16),
    "launch_claimed": ("integer", 0),
    "stop_requested": ("integer", 0),
    "uncertain": ("integer", 0),
    "reason": ("text", 256),
    "slot": ("integer", 0),
}
JOURNAL_CATALOG_QUERY = f"SELECT {eq._projection(eq._CATALOG)} FROM sqlite_schema LIMIT 4"
RUN_QUERY = (
    f"SELECT {cq._projection_real(_RUN)} FROM runs INDEXED BY sqlite_autoindex_runs_1 WHERE run_id=? LIMIT 2"
)
ACTIVE_QUERY = (
    "SELECT substr(CAST(run_id AS BLOB),1,37) AS run_id,typeof(run_id) AS run_id_kind "
    "FROM runs INDEXED BY one_active_run WHERE slot=1 AND state != 'stopped' LIMIT 2"
)
_SETUP = ("PRAGMA busy_timeout=0", "PRAGMA query_only=ON", "PRAGMA trusted_schema=OFF", "BEGIN")


class Refusal(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    COMPLETION_UNUSABLE = "COMPLETION_UNUSABLE"
    PATH_REFUSED = "PATH_REFUSED"
    SIDECAR_PRESENT = "SIDECAR_PRESENT"
    BUSY = "BUSY"
    SCHEMA_REFUSED = "SCHEMA_REFUSED"
    LEDGER_IDENTITY_MISMATCH = "LEDGER_IDENTITY_MISMATCH"
    CONTROL_MISMATCH = "CONTROL_MISMATCH"
    QUEUE_NOT_EMPTY = "QUEUE_NOT_EMPTY"
    ROW_REFUSED = "ROW_REFUSED"
    JOURNAL_INELIGIBLE = "JOURNAL_INELIGIBLE"
    ASSOCIATION_MISMATCH = "ASSOCIATION_MISMATCH"
    CAPTURE_ABORTED = "CAPTURE_ABORTED"
    EXPIRED = "EXPIRED"


class Fault(StrEnum):
    FILE_FAILURE = "FILE_FAILURE"
    SQLITE_FAILURE = "SQLITE_FAILURE"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"
    CLOSE_FAILURE = "CLOSE_FAILURE"


@dataclass(frozen=True, slots=True)
class CaptureAssociation:
    run_id: str
    capture_id: str
    journal_id: str
    principal: str
    uid: int
    boot_id: str
    worker_invocation_id: str
    worker_cgroup: str
    guard_boot_id: str
    guard_invocation_id: str
    guard_deadline_boottime: float
    guard_deadline_utc: float
    assignment: bytes | None


@dataclass(frozen=True, slots=True)
class AssociationResult:
    association: CaptureAssociation | None
    refusal: Refusal | None
    fault: Fault | None
    journal_cleanup: CleanupOutcome
    ledger_cleanup: CleanupOutcome


class _Refused(ValueError):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


class _Faulted(RuntimeError):
    def __init__(self, reason: Fault) -> None:
        self.reason = reason
        super().__init__(reason)


def _require(condition: bool, refusal: Refusal = Refusal.ROW_REFUSED) -> None:
    if not condition:
        raise _Refused(refusal)


@dataclass(frozen=True, slots=True)
class _Inputs:
    journal_path: Path
    ledger_path: Path
    grant: tuple[str, str, str, int, str, int | float, int | float]
    control: ExpectedControl
    snapshot_text: str
    snapshot: dict[str, Any]
    assignment: bytes | None
    guard: qa.ArmTuple
    clock: tuple[str, int | float, int | float]
    grace: float


def _uuid(value: object) -> str:
    try:
        return eq._uuid(value, eq.Refusal.INVALID_INPUT)
    except eq._Refused:
        raise _Refused(Refusal.INVALID_INPUT) from None


def _number(value: object) -> int | float:
    bad = Refusal.INVALID_INPUT
    if type(value) is int:
        _require(0 <= value <= MAX_INT, bad)
        return value
    _require(type(value) is float, bad)
    assert isinstance(value, float)
    _require(math.isfinite(value) and value >= 0, bad)
    return 0.0 if value == 0 else value


def _path(value: object) -> Path:
    try:
        return eq._path(value)
    except eq._Refused:
        raise _Refused(Refusal.INVALID_INPUT) from None


def _inputs(
    journal_path: object,
    ledger_path: object,
    expected: object,
    control: object,
    completion: object,
    guard: object,
    now: object,
    grace: object,
) -> _Inputs:
    bad = Refusal.INVALID_INPUT
    journal, ledger = _path(journal_path), _path(ledger_path)
    _require(type(expected) is Grant, bad)
    assert isinstance(expected, Grant)
    run_id, boot_id = _uuid(expected.run_id), _uuid(expected.boot_id)
    fence, principal, uid = expected.fence, expected.principal, expected.uid
    _require(type(fence) is str and FENCE.fullmatch(fence) is not None, bad)
    _require(type(principal) is str and len(principal) <= cq.PRINCIPAL_MAX, bad)
    _require(principal.isascii() and cq.PRINCIPAL.fullmatch(principal) is not None, bad)
    _require(type(uid) is int and 1 <= uid <= MAX_INT, bad)
    grant = (
        run_id,
        fence,
        principal,
        uid,
        boot_id,
        _number(expected.deadline_boottime),
        _number(expected.deadline_utc),
    )
    try:
        checked_control = eq._control(control)
    except eq._Refused:
        raise _Refused(bad) from None
    _require(type(completion) is cq.CapturedCompletion, bad)
    assert isinstance(completion, cq.CapturedCompletion)
    text, assignment = completion.snapshot, completion.assignment
    _require(type(text) is str, bad)
    _require(
        assignment is None or (type(assignment) is bytes and len(assignment) <= MAX_ASSIGNMENT_BYTES), bad
    )
    _require(type(now) is ClockReading, bad)
    assert isinstance(now, ClockReading)
    clock = (_uuid(now.boot_id), _number(now.boottime), _number(now.utc))
    _require(type(grace) is float and math.isfinite(grace) and grace >= 0, bad)
    assert isinstance(grace, float)
    try:
        checked_guard = qa._tuple(guard, qa.Refusal.INVALID_INPUT)
    except qa._Refused:
        raise _Refused(bad) from None
    _require(text.isascii() and len(text) <= cq.SNAPSHOT_MAX, Refusal.COMPLETION_UNUSABLE)
    try:
        _, snapshot = cq._snapshot_packet(text.encode("ascii"))
    except cq._Refused:
        raise _Refused(Refusal.COMPLETION_UNUSABLE) from None
    return _Inputs(
        journal, ledger, grant, checked_control, text, snapshot, assignment, checked_guard, clock, grace
    )


def _agree(inputs: _Inputs) -> None:
    mismatch = Refusal.ASSOCIATION_MISMATCH
    run_id, _, principal, _, boot_id, boottime, utc = inputs.grant
    control, snapshot, guard = inputs.control, inputs.snapshot, inputs.guard
    _require(boot_id == inputs.clock[0], mismatch)
    _require(
        snapshot["journal_id"] == control.journal_id
        and snapshot["run_id"] == run_id
        and snapshot["principal"] == principal
        and snapshot["counter"] == control.acked_counter,
        mismatch,
    )
    _require(
        guard.run_id == run_id
        and guard.guard_boot_id == boot_id
        and guard.deadline_boottime == boottime
        and guard.deadline_utc == utc,
        mismatch,
    )


@dataclass
class _Connection:
    conn: sqlite3.Connection | None = None
    begun: bool = False
    cleanup: CleanupOutcome = CleanupOutcome.CLOSED


@dataclass
class _State:
    journal: _Connection = field(default_factory=_Connection)
    ledger: _Connection = field(default_factory=_Connection)
    refusal: Refusal | None = None
    fault: Fault | None = None
    any_begun: bool = False

    def failed(self, fault: Fault) -> None:
        priority = {Fault.CLOSE_FAILURE: 3, Fault.ROLLBACK_FAILURE: 2}
        if self.fault is None or priority.get(fault, 1) > priority.get(self.fault, 1):
            self.fault = fault


def _exact_mode(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except OSError:
        raise _Refused(Refusal.PATH_REFUSED) from None
    _require(mode == 0o600, Refusal.PATH_REFUSED)


def _preflight(path: Path, slot: _Connection) -> tuple[int, int]:
    probe = eq._Attempt()
    try:
        _exact_mode(path)
        return eq._existing(path, probe)
    except eq._Refused as error:
        raise _Refused(Refusal(error.reason.value)) from None
    except StateError:
        if probe.cleanup == CleanupOutcome.UNCERTAIN:
            slot.cleanup = CleanupOutcome.UNCERTAIN
            raise _Faulted(Fault.CLOSE_FAILURE) from None
        raise _Refused(Refusal.PATH_REFUSED) from None
    except OSError:
        if probe.cleanup == CleanupOutcome.UNCERTAIN:
            slot.cleanup = CleanupOutcome.UNCERTAIN
            raise _Faulted(Fault.CLOSE_FAILURE) from None
        raise _Faulted(Fault.FILE_FAILURE) from None


def _open(path: Path, identity: tuple[int, int], slot: _Connection) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=0)
    slot.conn = conn
    conn.row_factory = sqlite3.Row
    try:
        check_private_file(path)
        metadata = path.lstat()
    except (StateError, OSError):
        raise _Refused(Refusal.PATH_REFUSED) from None
    _require((metadata.st_dev, metadata.st_ino) == identity, Refusal.PATH_REFUSED)
    _require(stat.S_IMODE(metadata.st_mode) == 0o600, Refusal.PATH_REFUSED)
    return conn


def _setup(conn: sqlite3.Connection, slot: _Connection, state: _State) -> None:
    for statement in _SETUP:
        conn.execute(statement)
    slot.begun = True
    state.any_begun = True


def _journal_catalog(conn: sqlite3.Connection) -> None:
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
            _require(isinstance(name, str) and name not in actual)
            actual[name] = (
                row["type"],
                row["tbl_name"],
                eq._sql(statement) if isinstance(statement, str) else None,
            )
    except (eq._Refused, _Refused):
        raise _Refused(Refusal.SCHEMA_REFUSED) from None
    _require(actual == _JOURNAL_CATALOG, Refusal.SCHEMA_REFUSED)


def _ledger_catalog(conn: sqlite3.Connection, control: ExpectedControl) -> None:
    try:
        eq._validate(conn)
        eq._check_control(conn, control)
    except eq._Refused as error:
        raise _Refused(Refusal(error.reason.value)) from None
    except eq._Failed:
        raise _Faulted(Fault.SQLITE_FAILURE) from None
    _require(control.active_seq is None, Refusal.QUEUE_NOT_EMPTY)


def _run(conn: sqlite3.Connection, inputs: _Inputs) -> None:
    run_id, fence, principal, uid, boot_id, boottime, utc = inputs.grant
    rows = conn.execute(RUN_QUERY, (run_id,)).fetchall()
    _require(len(rows) == 1)
    try:
        row = cq._row(rows[0], _RUN, frozenset(("reason",)))
    except cq._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None
    _require(row["run_id"] == run_id)
    for key in ("deadline_boottime", "deadline_utc"):
        _require(math.isfinite(row[key]) and row[key] >= 0)
    for key in ("launch_claimed", "stop_requested", "uncertain"):
        _require(row[key] in (0, 1))
    _require(
        row["slot"] == 1 and row["state"] in ("reserved", "starting", "stopping", "quarantined", "stopped")
    )
    _require(
        (
            row["fence"],
            row["principal"],
            row["uid"],
            row["boot_id"],
            row["deadline_boottime"],
            row["deadline_utc"],
        )
        == (fence, principal, uid, boot_id, boottime, utc),
        Refusal.ASSOCIATION_MISMATCH,
    )
    _require(
        (row["state"], row["launch_claimed"], row["stop_requested"], row["uncertain"], row["reason"])
        == ("starting", 1, 0, 0, None),
        Refusal.JOURNAL_INELIGIBLE,
    )
    active = conn.execute(ACTIVE_QUERY).fetchall()
    _require(len(active) == 1, Refusal.JOURNAL_INELIGIBLE)
    _require(active[0]["run_id_kind"] == "text" and type(active[0]["run_id"]) is bytes)
    _require(active[0]["run_id"] == run_id.encode("ascii"), Refusal.JOURNAL_INELIGIBLE)
    _, clock_boottime, clock_utc = inputs.clock
    grace = inputs.grace
    _require(clock_boottime < boottime - grace and clock_utc < utc - grace, Refusal.EXPIRED)


def _ledger(conn: sqlite3.Connection, inputs: _Inputs) -> CaptureAssociation:
    mismatch = Refusal.ASSOCIATION_MISMATCH
    run_id, fence, principal, uid, boot_id, _, deadline_utc = inputs.grant
    control, snapshot = inputs.control, inputs.snapshot
    capture_id = snapshot["capture_id"]
    try:
        stored = eq._binding(conn, run_id)
    except eq._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None
    _require(stored is not None, mismatch)
    assert stored is not None
    claim, acked = stored
    _require(claim.uid == uid and claim.boot_id == boot_id and acked, mismatch)
    choices = _capture(conn, run_id, capture_id, principal, inputs.snapshot_text)
    _receipt(conn, capture_id, run_id, fence, claim, control, snapshot)
    _require(conn.execute(cq.LINK_BY_PARENT_QUERY, (capture_id,)).fetchall() == [], Refusal.CAPTURE_ABORTED)
    try:
        arm = qa._current(conn, run_id)
    except qa._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None
    _require(arm == inputs.guard, mismatch)
    try:
        derived = cq._derive(snapshot, run_id, choices[0])
    except cq._Refused:
        raise _Refused(Refusal.COMPLETION_UNUSABLE) from None
    _require(derived == choices[1] == inputs.assignment, mismatch)
    effective = min(float(snapshot["deadline"]), choices[0].cutoff_utc)
    _require(effective <= deadline_utc, mismatch)
    _require(effective > inputs.clock[2] + inputs.grace, Refusal.EXPIRED)
    guard = inputs.guard
    return CaptureAssociation(
        run_id,
        capture_id,
        control.journal_id,
        principal,
        uid,
        boot_id,
        claim.invocation_id,
        claim.cgroup,
        guard.guard_boot_id,
        guard.guard_invocation_id,
        guard.deadline_boottime,
        guard.deadline_utc,
        inputs.assignment,
    )


def _capture(
    conn: sqlite3.Connection, run_id: str, capture_id: str, principal: str, snapshot_text: str
) -> tuple[cq.CaptureChoices, bytes | None]:
    rows = conn.execute(cq.CAPTURES_QUERY, (run_id,)).fetchall()
    _require(len(rows) == 1)
    try:
        row = cq._row(rows[0], cq._CAPTURES, frozenset(("snapshot", "assignment")))
        policy = cq._strict_row_json(row["policy"])
        _require(frozenset(policy) == cq._POLICY_FIELDS)
        choices = cq._choices(cq.CaptureChoices(row["principal"], row["cutoff_utc"], NightPolicy(**policy)))
    except (cq._Refused, AssignmentError, TypeError):
        raise _Refused(Refusal.ROW_REFUSED) from None
    _require(row["run_id"] == run_id and cq._policy_text(choices.policy) == row["policy"])
    _require(row["receipt_id"] == capture_id, Refusal.ASSOCIATION_MISMATCH)
    _require(row["principal"] == principal, Refusal.ASSOCIATION_MISMATCH)
    _require(row["snapshot"] == snapshot_text, Refusal.ASSOCIATION_MISMATCH)
    assignment = row["assignment"]
    _require(assignment is None or type(assignment) is bytes)
    return choices, assignment


def _receipt(
    conn: sqlite3.Connection,
    capture_id: str,
    run_id: str,
    fence: str,
    claim: BindingClaim,
    control: ExpectedControl,
    snapshot: dict[str, Any],
) -> None:
    mismatch = Refusal.ASSOCIATION_MISMATCH
    try:
        row = cq._receipt(conn.execute(cq.RECEIPT_BY_ID_QUERY, (capture_id,)).fetchall())
        payload = cq._strict_row_json(row["payload"])
    except cq._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None
    _require(row["receipt_id"] == capture_id and 1 <= row["seq"] <= MAX_INT and row["acked"] in (0, 1))
    _require(frozenset(payload) == cq._REQUEST_FIELDS)
    _require((row["run_id"], row["action"], row["acked"]) == (run_id, "capture", 1), mismatch)
    _require(type(payload["counter"]) is int and payload["counter"] == snapshot["counter"] - 1, mismatch)
    _require(control.acked_counter == snapshot["counter"], mismatch)
    _require(
        (payload["journal_id"], payload["fence"], payload["receipt_id"])
        == (control.journal_id, fence, capture_id)
        and (payload["boot_id"], payload["invocation_id"], payload["cgroup"])
        == (claim.boot_id, claim.invocation_id, claim.cgroup),
        mismatch,
    )


def _in_transaction(state: _State) -> bool:
    for slot in (state.journal, state.ledger):
        if slot.conn is None:
            continue
        try:
            if slot.conn.in_transaction:
                return True
        except sqlite3.Error:
            return True
    return False


def _finish(slot: _Connection, state: _State) -> None:
    conn = slot.conn
    if conn is None:
        return
    try:
        if slot.begun or conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        slot.cleanup = CleanupOutcome.UNCERTAIN
        state.failed(Fault.ROLLBACK_FAILURE)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            slot.cleanup = CleanupOutcome.UNCERTAIN
            state.failed(Fault.CLOSE_FAILURE)


def read_capture_association(
    journal_path: Path,
    ledger_path: Path,
    *,
    expected: Grant,
    control: ExpectedControl,
    completion: cq.CapturedCompletion,
    guard: qa.ArmTuple,
    now: ClockReading,
    grace: float,
) -> AssociationResult:
    state = _State()
    association: CaptureAssociation | None = None
    try:
        try:
            inputs = _inputs(journal_path, ledger_path, expected, control, completion, guard, now, grace)
        except AttributeError:
            raise _Refused(Refusal.INVALID_INPUT) from None
        _agree(inputs)
        journal_identity = _preflight(inputs.journal_path, state.journal)
        ledger_identity = _preflight(inputs.ledger_path, state.ledger)
        _require(journal_identity != ledger_identity, Refusal.PATH_REFUSED)
        journal = _open(inputs.journal_path, journal_identity, state.journal)
        ledger = _open(inputs.ledger_path, ledger_identity, state.ledger)
        _setup(journal, state.journal, state)
        _setup(ledger, state.ledger, state)
        _journal_catalog(journal)
        _ledger_catalog(ledger, inputs.control)
        _run(journal, inputs)
        association = _ledger(ledger, inputs)
    except _Refused as error:
        state.refusal = error.reason
    except _Faulted as error:
        state.failed(error.reason)
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if (
            not state.any_begun
            and not _in_transaction(state)
            and type(code) is int
            and code >= 0
            and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        ):
            state.refusal = Refusal.BUSY
        else:
            state.failed(Fault.SQLITE_FAILURE)
    except (StateError, OSError, ValueError, TypeError, OverflowError):
        state.failed(Fault.FILE_FAILURE)
    finally:
        _finish(state.ledger, state)
        _finish(state.journal, state)
    usable = (
        state.refusal is None
        and state.fault is None
        and state.journal.cleanup == CleanupOutcome.CLOSED
        and state.ledger.cleanup == CleanupOutcome.CLOSED
    )
    return AssociationResult(
        association if usable else None,
        state.refusal,
        state.fault,
        state.journal.cleanup,
        state.ledger.cleanup,
    )
