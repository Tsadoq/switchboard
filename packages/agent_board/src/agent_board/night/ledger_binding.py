import os
import re
import sqlite3
from dataclasses import astuple, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.ledger import _SCHEMA
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import StateError, _check_file, check_private_file


class Refusal(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    SIDECAR_PRESENT = "SIDECAR_PRESENT"
    BUSY = "BUSY"
    LEDGER_IDENTITY_MISMATCH = "LEDGER_IDENTITY_MISMATCH"
    BINDING_CONFLICT = "BINDING_CONFLICT"
    SCHEMA_REFUSED = "SCHEMA_REFUSED"
    ROW_REFUSED = "ROW_REFUSED"


class Fault(StrEnum):
    FILE_FAILURE = "FILE_FAILURE"
    SQLITE_FAILURE = "SQLITE_FAILURE"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"
    CLOSE_FAILURE = "CLOSE_FAILURE"
    SNAPSHOT_FAILURE = "SNAPSHOT_FAILURE"


@dataclass(frozen=True)
class BindingClaim:
    run_id: str
    uid: int
    boot_id: str
    invocation_id: str
    cgroup: str


@dataclass(frozen=True)
class StoredBinding:
    journal_id: str
    claim: BindingClaim
    observed_server_acked: bool


@dataclass(frozen=True)
class BindingWriteResult:
    mutation: MutationOutcome
    cleanup: CleanupOutcome
    changed: bool | None
    stored: StoredBinding | None
    refusal: Refusal | None
    fault: Fault | None


class _Refused(ValueError):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


class _Failed(ValueError):
    def __init__(self, reason: Fault) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class _Attempt:
    mutation: MutationOutcome = MutationOutcome.NOT_ATTEMPTED
    cleanup: CleanupOutcome = CleanupOutcome.CLOSED
    refusal: Refusal | None = None
    fault: Fault | None = None
    begun: bool = False
    commit_attempted: bool = False
    rollback_attempted: bool = False
    changed: bool = False
    finished: bool = False
    candidate: StoredBinding | None = None

    def failed(self, fault: Fault) -> None:
        priority = {Fault.CLOSE_FAILURE: 3, Fault.ROLLBACK_FAILURE: 2}
        if self.fault is None or priority.get(fault, 1) > priority.get(self.fault, 1):
            self.fault = fault

    def result(self) -> BindingWriteResult:
        changed = self.changed if self.mutation == MutationOutcome.COMMITTED else False
        usable = (
            self.finished
            and self.cleanup == CleanupOutcome.CLOSED
            and self.refusal is None
            and self.fault is None
        )
        return BindingWriteResult(
            self.mutation,
            self.cleanup,
            None if self.mutation == MutationOutcome.UNKNOWN else changed,
            self.candidate if usable else None,
            self.refusal,
            self.fault,
        )


def _require(condition: bool, refusal: Refusal = Refusal.ROW_REFUSED) -> None:
    if not condition:
        raise _Refused(refusal)


def _uuid(value: object) -> str:
    _require(type(value) is str and isinstance(value, str) and len(value) == 36)
    if not isinstance(value, str):
        raise _Refused(Refusal.ROW_REFUSED)
    try:
        _require(str(UUID(value)) == value)
    except ValueError:
        raise _Refused(Refusal.ROW_REFUSED) from None
    return value


def _claim(value: BindingClaim) -> BindingClaim:
    _require(type(value) is BindingClaim)
    run_id, boot_id = _uuid(value.run_id), _uuid(value.boot_id)
    _require(type(value.uid) is int and 1 <= value.uid <= 2**63 - 1)
    _require(
        type(value.invocation_id) is str
        and re.fullmatch("[0-9a-f]{32}", value.invocation_id) is not None
        and value.invocation_id != "0" * 32
    )
    _require(type(value.cgroup) is str and len(value.cgroup) <= 4096)
    _require(len(value.cgroup.encode("utf-8")) <= 4096)
    _require(value.cgroup == unit_cgroup(f"switchboard-night-{run_id}.service"))
    return BindingClaim(run_id, value.uid, boot_id, value.invocation_id, value.cgroup)


_IDENTITY = {"singleton": ("integer", 0), "journal_id": ("text", 36), "acked_counter": ("integer", 0)}
_BINDING = {
    "run_id": ("text", 36),
    "uid": ("integer", 0),
    "boot_id": ("text", 36),
    "invocation_id": ("text", 32),
    "cgroup": ("text", 4096),
    "server_acked": ("integer", 0),
}
_CATALOG = {"name": ("text", 128), "type": ("text", 16), "tbl_name": ("text", 128), "sql": ("text", 8192)}


def _projection(spec: dict[str, tuple[str, int]]) -> str:
    columns: list[str] = []
    for name, (kind, cap) in spec.items():
        value = (
            f"substr(CAST({name} AS BLOB),1,{cap + 1})"
            if cap
            else f"CASE WHEN typeof({name})='{kind}' THEN {name} END"
        )
        columns.extend((f"{value} AS {name}", f"typeof({name}) AS {name}_kind"))
    return ",".join(columns)


IDENTITY_QUERY = f"SELECT {_projection(_IDENTITY)} FROM identity LIMIT 2"
BINDING_QUERY = f"SELECT {_projection(_BINDING)} FROM bindings INDEXED BY sqlite_autoindex_bindings_1 WHERE run_id=? LIMIT 2"
CATALOG_QUERY = f"SELECT {_projection(_CATALOG)} FROM sqlite_schema LIMIT 17"


def _row(
    row: sqlite3.Row,
    spec: dict[str, tuple[str, int]],
    *,
    nullable: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, (kind, cap) in spec.items():
        value, actual = row[name], row[f"{name}_kind"]
        if actual == "null" and name in nullable:
            result[name] = None
            continue
        _require(actual == kind)
        if cap:
            _require(type(value) is bytes and len(value) <= cap)
            try:
                result[name] = value.decode("utf-8")
            except UnicodeError:
                raise _Refused(Refusal.ROW_REFUSED) from None
        else:
            _require(type(value) is (int if kind == "integer" else float))
            result[name] = value
    return result


def _sql(statement: str) -> str:
    return " ".join(re.sub(r"\bIF NOT EXISTS\b", "", statement).split())


def _validate(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise _Failed(Fault.SNAPSHOT_FAILURE)
    for statement, expected in (("PRAGMA journal_mode", "delete"), ("PRAGMA user_version", 3)):
        rows = conn.execute(statement).fetchall()
        _require(
            len(rows) == 1 and type(rows[0][0]) is type(expected) and rows[0][0] == expected,
            Refusal.SCHEMA_REFUSED,
        )
    expected_objects: dict[str, tuple[str, str, str | None]] = {}
    for statement in _SCHEMA:
        match = re.match(r"CREATE (?:UNIQUE )?(TABLE|INDEX) IF NOT EXISTS (\w+)", statement)
        if match is not None:
            name, kind = match[2], match[1].lower()
            table = name if kind == "table" else statement.split(" ON ", 1)[1].split()[0]
            expected_objects[name] = kind, table, _sql(statement)
    for table, count in (
        ("receipts", 1),
        ("capture_aborts", 2),
        ("captures", 2),
        ("bindings", 1),
        ("arms", 1),
    ):
        for index in range(1, count + 1):
            expected_objects[f"sqlite_autoindex_{table}_{index}"] = "index", table, None
    expected_objects["sqlite_sequence"] = "table", "sqlite_sequence", "CREATE TABLE sqlite_sequence(name,seq)"
    rows = conn.execute(CATALOG_QUERY).fetchall()
    _require(len(rows) == 16, Refusal.SCHEMA_REFUSED)
    actual_objects: dict[str, tuple[object, object, object]] = {}
    try:
        for raw in rows:
            row = _row(raw, _CATALOG, nullable=frozenset(("sql",)))
            name, statement = row["name"], row["sql"]
            _require(isinstance(name, str) and name not in actual_objects)
            if not isinstance(name, str):
                raise _Refused(Refusal.SCHEMA_REFUSED)
            actual_objects[name] = (
                row["type"],
                row["tbl_name"],
                _sql(statement) if isinstance(statement, str) else None,
            )
    except _Refused:
        raise _Refused(Refusal.SCHEMA_REFUSED) from None
    _require(actual_objects == expected_objects, Refusal.SCHEMA_REFUSED)


def _identity(conn: sqlite3.Connection, journal_id: str) -> None:
    rows = conn.execute(IDENTITY_QUERY).fetchall()
    _require(len(rows) == 1)
    row = _row(rows[0], _IDENTITY, nullable=frozenset(("journal_id",)))
    counter = row["acked_counter"]
    _require(row["singleton"] == 1 and type(counter) is int and 0 <= counter <= 2**63 - 1)
    if row["journal_id"] is not None:
        _uuid(row["journal_id"])
    _require(row["journal_id"] == journal_id, Refusal.LEDGER_IDENTITY_MISMATCH)


def _current(conn: sqlite3.Connection, run_id: str, journal_id: str) -> StoredBinding | None:
    rows = conn.execute(BINDING_QUERY, (run_id,)).fetchall()
    _require(len(rows) <= 1)
    if not rows:
        return None
    values = _row(rows[0], _BINDING)
    observed = values.pop("server_acked")
    _require(observed in (0, 1))
    claim = _claim(BindingClaim(**values))
    _require(claim.run_id == run_id)
    return StoredBinding(journal_id, claim, bool(observed))


def _close_fd(fd: int, attempt: _Attempt) -> None:
    try:
        os.close(fd)
    except OSError:
        attempt.cleanup = CleanupOutcome.UNCERTAIN
        attempt.failed(Fault.CLOSE_FAILURE)
        raise


def _existing(path: Path, attempt: _Attempt) -> tuple[int, int]:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path.anchor != "/"
        or path != Path(os.path.normpath(path))
    ):
        raise StateError("invalid_state_path")
    check_private_file(path)
    metadata = path.lstat()
    identity = metadata.st_dev, metadata.st_ino
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        raise _Refused(Refusal.SIDECAR_PRESENT)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        _check_file(opened)
        if (opened.st_dev, opened.st_ino) != identity:
            raise StateError("ledger_replaced")
        header = os.read(fd, 100)
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise StateError("unsupported_ledger_header")
    finally:
        _close_fd(fd, attempt)
    return identity


def _rollback(conn: sqlite3.Connection, attempt: _Attempt) -> None:
    attempt.rollback_attempted = True
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        attempt.failed(Fault.ROLLBACK_FAILURE)
        raise
    if attempt.begun and not attempt.commit_attempted:
        attempt.mutation = MutationOutcome.ROLLED_BACK


def _finish(conn: sqlite3.Connection, attempt: _Attempt) -> None:
    try:
        if not attempt.rollback_attempted and conn.in_transaction:
            _rollback(conn, attempt)
    except sqlite3.Error:
        attempt.failed(Fault.ROLLBACK_FAILURE)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            attempt.cleanup = CleanupOutcome.UNCERTAIN
            attempt.failed(Fault.CLOSE_FAILURE)
        else:
            if attempt.begun and not attempt.commit_attempted:
                attempt.mutation = MutationOutcome.ROLLED_BACK


def publish_binding(
    path: Path,
    *,
    journal_id: str,
    claim: BindingClaim,
) -> BindingWriteResult:
    attempt = _Attempt()
    conn: sqlite3.Connection | None = None
    try:
        try:
            journal_id, claim = _uuid(journal_id), _claim(claim)
        except (_Refused, AttributeError, TypeError, ValueError):
            raise _Refused(Refusal.INVALID_INPUT) from None
        identity = _existing(path, attempt)
        conn = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=0)
        conn.row_factory = sqlite3.Row
        check_private_file(path)
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity:
            raise StateError("ledger_replaced")
        for statement in ("PRAGMA busy_timeout=0", "PRAGMA trusted_schema=OFF", "PRAGMA synchronous=EXTRA"):
            conn.execute(statement)
        conn.execute("BEGIN IMMEDIATE")
        attempt.begun = True
        attempt.mutation = MutationOutcome.UNKNOWN
        _validate(conn)
        _identity(conn, journal_id)
        current = _current(conn, claim.run_id, journal_id)
        if current is not None:
            _require(current.claim == claim, Refusal.BINDING_CONFLICT)
            attempt.candidate = current
            _rollback(conn, attempt)
        else:
            cursor = conn.execute(
                "INSERT INTO bindings(run_id,uid,boot_id,invocation_id,cgroup,server_acked) VALUES(?,?,?,?,?,0)",
                astuple(claim),
            )
            if cursor.rowcount != 1:
                raise _Failed(Fault.SNAPSHOT_FAILURE)
            attempt.changed = True
            try:
                stored = _current(conn, claim.run_id, journal_id)
            except _Refused:
                raise _Failed(Fault.SNAPSHOT_FAILURE) from None
            if stored != StoredBinding(journal_id, claim, False):
                raise _Failed(Fault.SNAPSHOT_FAILURE)
            attempt.candidate = stored
            attempt.commit_attempted = True
            conn.execute("COMMIT")
            attempt.mutation = MutationOutcome.COMMITTED
        attempt.finished = True
    except _Refused as error:
        attempt.refusal = error.reason
    except _Failed as error:
        attempt.failed(error.reason)
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if (
            not attempt.begun
            and type(code) is int
            and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        ):
            attempt.refusal = Refusal.BUSY
        else:
            attempt.failed(Fault.SQLITE_FAILURE)
    except (StateError, OSError, ValueError, TypeError, OverflowError):
        attempt.failed(Fault.FILE_FAILURE)
    finally:
        if conn is not None:
            _finish(conn, attempt)
    return attempt.result()
