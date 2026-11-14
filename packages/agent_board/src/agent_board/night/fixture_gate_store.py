import os
import sqlite3
from dataclasses import astuple, dataclass
from enum import StrEnum
from pathlib import Path

from agent_board.night.fixture_gate_dto import (
    BootstrapSelector,
    Context,
    FixtureBootstrap,
    InvalidRecord,
    NormalAttestation,
    RuntimeOwner,
    RuntimeSelector,
    Selector,
    decode_record,
    encode_record,
)
from agent_board.night.fixture_gate_sql import (
    CONTEXT_COLUMNS,
    DDL,
    FAULT_REASONS,
    Observation,
    StoreRefusal,
    _Current,
    _current,
    _validate,
)
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night_state import StateError, _check_ancestors, _check_file, check_private_file


class Refusal(StrEnum):
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    SCHEMA_REFUSED = "SCHEMA_REFUSED"
    STATE_MISMATCH = "STATE_MISMATCH"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    REPLAY_REFUSED = "REPLAY_REFUSED"
    ATTESTATION_REFUSED = "ATTESTATION_REFUSED"
    PREEXISTING_PATH = "PREEXISTING_PATH"


class Fault(StrEnum):
    FILE_FAILURE = "FILE_FAILURE"
    SQLITE_FAILURE = "SQLITE_FAILURE"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"
    CLOSE_FAILURE = "CLOSE_FAILURE"
    SNAPSHOT_FAILURE = "SNAPSHOT_FAILURE"
    DIRECTORY_PUBLICATION_FAILURE = "DIRECTORY_PUBLICATION_FAILURE"


class Publication(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class WriteResult:
    mutation: MutationOutcome
    cleanup: CleanupOutcome
    changed: bool | None
    refusal: Refusal | None
    fault: Fault | None


@dataclass(frozen=True)
class ReadResult:
    observation: Observation | None
    cleanup: CleanupOutcome
    refusal: Refusal | None
    fault: Fault | None


@dataclass(frozen=True)
class ProvisionResult(WriteResult):
    observed_created_path: bool
    path_publication: Publication


@dataclass
class _Execution:
    mutation: MutationOutcome = MutationOutcome.NOT_ATTEMPTED
    cleanup: CleanupOutcome = CleanupOutcome.CLOSED
    refusal: Refusal | None = None
    fault: Fault | None = None
    observation: Observation | None = None
    begun: bool = False
    commit_attempted: bool = False
    rollback_attempted: bool = False
    created_path: bool = False
    publication: Publication = Publication.NOT_ATTEMPTED

    def write_result(self) -> WriteResult:
        changed = (
            None if self.mutation == MutationOutcome.UNKNOWN else self.mutation == MutationOutcome.COMMITTED
        )
        return WriteResult(self.mutation, self.cleanup, changed, self.refusal, self.fault)


_CONTEXT_WHERE = " AND ".join(f"{name}=?" for name in CONTEXT_COLUMNS)
_CONTROL_WHERE = f"id=1 AND {_CONTEXT_WHERE} AND state=? AND operation=? AND launch=?"
_IDENTITY_WHERE = (
    f"{_CONTEXT_WHERE} AND operation=? AND launch=? AND run_id=? AND fence_sha256=? AND status='PENDING'"
)


def _require(condition: bool, reason: Refusal = Refusal.STATE_MISMATCH) -> None:
    if not condition:
        raise StoreRefusal(reason)


def _input_context(context: Context) -> Context:
    _require(type(context) is Context, Refusal.CONTEXT_MISMATCH)
    try:
        return Context(*astuple(context))
    except (InvalidRecord, AttributeError, TypeError):
        raise StoreRefusal(Refusal.CONTEXT_MISMATCH) from None


def _input_owner(owner: RuntimeOwner | None) -> RuntimeOwner:
    if type(owner) is not RuntimeOwner:
        raise StoreRefusal(Refusal.OWNER_MISMATCH)
    try:
        return RuntimeOwner(*astuple(owner))
    except (InvalidRecord, AttributeError, TypeError):
        raise StoreRefusal(Refusal.OWNER_MISMATCH) from None


def _close_fd(fd: int, execution: _Execution) -> None:
    try:
        os.close(fd)
    except OSError:
        execution.cleanup = CleanupOutcome.UNCERTAIN
        execution.fault = Fault.CLOSE_FAILURE
        raise


def _path(path: Path) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path.anchor != "/"
        or path != Path(os.path.normpath(path))
    ):
        raise StateError("invalid_state_path")
    _check_ancestors(path.parent)


def _sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        raise StateError("gate_sidecar_requires_maintenance")


def _existing(path: Path, execution: _Execution) -> tuple[int, int]:
    _path(path)
    _sidecars(path)
    check_private_file(path)
    metadata = path.lstat()
    identity = metadata.st_dev, metadata.st_ino
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        _check_file(opened)
        if (opened.st_dev, opened.st_ino) != identity:
            raise StateError("gate_replaced")
        header = os.read(fd, 100)
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise StateError("unsupported_gate_header")
    finally:
        _close_fd(fd, execution)
    return identity


def _directory_sync(path: Path, execution: _Execution, *, final: bool) -> None:
    execution.fault = Fault.DIRECTORY_PUBLICATION_FAILURE
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
        if final:
            execution.publication = Publication.CONFIRMED
        execution.fault = None
    finally:
        _close_fd(fd, execution)


def _create(path: Path, execution: _Execution) -> tuple[int, int]:
    _path(path)
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise StoreRefusal(Refusal.PREEXISTING_PATH)
    _sidecars(path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        raise StoreRefusal(Refusal.PREEXISTING_PATH) from None
    execution.created_path = True
    execution.publication = Publication.UNKNOWN
    try:
        metadata = os.fstat(fd)
        _check_file(metadata)
        identity = metadata.st_dev, metadata.st_ino
    finally:
        _close_fd(fd, execution)
    _directory_sync(path, execution, final=False)
    return identity


def _rowcount(cursor: sqlite3.Cursor) -> None:
    _require(cursor.rowcount == 1, Refusal.OWNER_MISMATCH)


def _initialize(conn: sqlite3.Connection, context: Context) -> None:
    for statement in DDL:
        conn.execute(statement)
    conn.execute("PRAGMA user_version=1")
    selector = BootstrapSelector()
    reference = context.reference(selector.operation, selector.launch)
    conn.execute(
        "INSERT INTO identities VALUES(?,?,?,?,?,?,?,?,?,?)",
        (selector.operation, selector.launch, *astuple(context), None, None, "BOOTSTRAP"),
    )
    conn.execute(
        "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            reference,
            selector.operation,
            selector.launch,
            *astuple(context),
            "FIXTURE_BOOTSTRAP_V1",
            encode_record(FixtureBootstrap(context)),
        ),
    )
    conn.execute(
        "INSERT INTO control VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (1, *astuple(context), "IDLE", selector.operation, selector.launch, reference, None),
    )


def _begin_operation(
    conn: sqlite3.Connection, context: Context, current: _Current, owner: RuntimeOwner
) -> None:
    _require(current.control["state"] == "IDLE")
    for table, column, index, value in (
        ("identities", "operation", 1, owner.operation),
        ("identities", "launch", 2, owner.launch),
        ("completions", "operation", 2, owner.operation),
        ("completions", "launch", 3, owner.launch),
    ):
        rows = conn.execute(
            f"SELECT 1 FROM {table} INDEXED BY sqlite_autoindex_{table}_{index} WHERE {column}=? LIMIT 2",
            (value,),
        ).fetchall()
        _require(not rows, Refusal.REPLAY_REFUSED)
    conn.execute(
        "INSERT INTO identities VALUES(?,?,?,?,?,?,?,?,?,?)",
        (owner.operation, owner.launch, *astuple(context), owner.run_id, owner.fence_sha256, "PENDING"),
    )
    _rowcount(
        conn.execute(
            "UPDATE control SET state='PENDING',operation=?,launch=?,completion_ref=NULL,fault_reason=NULL "
            f"WHERE {_CONTROL_WHERE}",
            (
                owner.operation,
                owner.launch,
                *astuple(context),
                "IDLE",
                current.control["operation"],
                current.control["launch"],
            ),
        )
    )


def _complete_operation(
    conn: sqlite3.Connection, context: Context, current: _Current, attestation: NormalAttestation
) -> None:
    owner = attestation.owner
    _require(current.control["state"] == "PENDING")
    _require(current.observation.selector == RuntimeSelector(owner), Refusal.OWNER_MISMATCH)
    reference = context.reference(owner.operation, owner.launch)
    conn.execute(
        "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            reference,
            owner.operation,
            owner.launch,
            *astuple(context),
            attestation.kind,
            encode_record(attestation),
        ),
    )
    _rowcount(
        conn.execute(
            f"UPDATE identities SET status='COMPLETED' WHERE {_IDENTITY_WHERE}",
            (*astuple(context), *astuple(owner)),
        )
    )
    _rowcount(
        conn.execute(
            f"UPDATE control SET state='IDLE',completion_ref=?,fault_reason=NULL WHERE {_CONTROL_WHERE}",
            (reference, *astuple(context), "PENDING", owner.operation, owner.launch),
        )
    )


def _lock_fault(
    conn: sqlite3.Connection,
    context: Context,
    current: _Current,
    selector: Selector,
    expected_state: str,
    reason: str,
) -> None:
    _require(current.control["state"] == expected_state)
    _require(current.observation.selector == selector, Refusal.OWNER_MISMATCH)
    if isinstance(selector, BootstrapSelector):
        _require(
            expected_state == "IDLE" and current.identity["status"] == "BOOTSTRAP", Refusal.OWNER_MISMATCH
        )
    elif expected_state == "PENDING":
        _rowcount(
            conn.execute(
                f"UPDATE identities SET status='FAULTED' WHERE {_IDENTITY_WHERE}",
                (*astuple(context), *astuple(selector.owner)),
            )
        )
    _rowcount(
        conn.execute(
            f"UPDATE control SET state='FAULT_LOCKED',completion_ref=NULL,fault_reason=? WHERE {_CONTROL_WHERE}",
            (
                reason,
                *astuple(context),
                expected_state,
                current.control["operation"],
                current.control["launch"],
            ),
        )
    )


def _finish(conn: sqlite3.Connection, execution: _Execution) -> None:
    try:
        if not execution.rollback_attempted and conn.in_transaction:
            execution.rollback_attempted = True
            conn.execute("ROLLBACK")
            if execution.begun and not execution.commit_attempted:
                execution.mutation = MutationOutcome.ROLLED_BACK
    except sqlite3.Error:
        execution.fault = Fault.ROLLBACK_FAILURE
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            execution.cleanup = CleanupOutcome.UNCERTAIN
            execution.fault = Fault.CLOSE_FAILURE
        else:
            if execution.begun and not execution.commit_attempted:
                execution.mutation = MutationOutcome.ROLLED_BACK


def _execute(
    path: Path,
    context: Context,
    action: str,
    *,
    owner: RuntimeOwner | None = None,
    attestation: NormalAttestation | None = None,
    selector: Selector | None = None,
    expected_state: str = "",
    reason: str = "",
) -> _Execution:
    execution = _Execution()
    conn: sqlite3.Connection | None = None
    try:
        context = _input_context(context)
        if action == "begin":
            owner = _input_owner(owner)
        elif action == "complete":
            _require(type(attestation) is NormalAttestation, Refusal.ATTESTATION_REFUSED)
            if attestation is not None:
                decode_record(encode_record(attestation), context=context, owner=attestation.owner)
        elif action == "fault":
            _require(type(selector) in (RuntimeSelector, BootstrapSelector), Refusal.OWNER_MISMATCH)
            _require(type(expected_state) is str and expected_state in ("IDLE", "PENDING"))
            _require(type(reason) is str and reason in FAULT_REASONS, Refusal.ATTESTATION_REFUSED)
        identity = _create(path, execution) if action == "provision" else _existing(path, execution)
        conn = sqlite3.connect(
            f"{path.as_uri()}?mode={'ro' if action == 'observe' else 'rw'}",
            uri=True,
            isolation_level=None,
            timeout=0,
        )
        conn.row_factory = sqlite3.Row
        check_private_file(path)
        opened = path.lstat()
        if (opened.st_dev, opened.st_ino) != identity:
            raise StateError("gate_replaced")
        for statement in (
            "PRAGMA busy_timeout=0",
            "PRAGMA trusted_schema=OFF",
            "PRAGMA foreign_keys=ON",
            "PRAGMA query_only=ON" if action == "observe" else "PRAGMA synchronous=EXTRA",
        ):
            conn.execute(statement)
        if action == "provision":
            conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN" if action == "observe" else "BEGIN IMMEDIATE")
        execution.begun = True
        execution.mutation = MutationOutcome.UNKNOWN
        if action == "provision":
            _initialize(conn, context)
        _validate(conn)
        current = _current(conn, context)
        if action == "begin" and owner is not None:
            _begin_operation(conn, context, current, owner)
        elif action == "complete" and attestation is not None:
            _complete_operation(conn, context, current, attestation)
        elif action == "fault" and selector is not None:
            _lock_fault(conn, context, current, selector, expected_state, reason)
        execution.commit_attempted = True
        conn.execute("COMMIT")
        execution.mutation = MutationOutcome.COMMITTED
        execution.observation = current.observation if action == "observe" else None
    except StoreRefusal as refused:
        if refused.reason == "SNAPSHOT_FAILURE":
            execution.fault = Fault.SNAPSHOT_FAILURE
        else:
            execution.refusal = Refusal(refused.reason)
    except InvalidRecord:
        execution.refusal = Refusal.ATTESTATION_REFUSED
    except sqlite3.Error:
        execution.fault = Fault.SQLITE_FAILURE
    except (StateError, OSError, ValueError, TypeError, OverflowError):
        execution.fault = execution.fault or Fault.FILE_FAILURE
    finally:
        if conn is not None:
            _finish(conn, execution)
    if action == "provision" and execution.mutation == MutationOutcome.COMMITTED and execution.fault is None:
        try:
            _directory_sync(path, execution, final=True)
        except OSError:
            pass
    return execution


def observe(path: Path, *, context: Context) -> ReadResult:
    execution = _execute(path, context, "observe")
    usable = (
        execution.cleanup == CleanupOutcome.CLOSED and execution.refusal is None and execution.fault is None
    )
    return ReadResult(
        execution.observation if usable else None, execution.cleanup, execution.refusal, execution.fault
    )


def provision_fixture(path: Path, *, context: Context) -> ProvisionResult:
    execution = _execute(path, context, "provision")
    result = execution.write_result()
    return ProvisionResult(
        result.mutation,
        result.cleanup,
        result.changed,
        result.refusal,
        result.fault,
        execution.created_path,
        execution.publication,
    )


def begin_operation(path: Path, *, context: Context, owner: RuntimeOwner) -> WriteResult:
    return _execute(path, context, "begin", owner=owner).write_result()


def complete_operation(path: Path, *, context: Context, attestation: NormalAttestation) -> WriteResult:
    return _execute(path, context, "complete", attestation=attestation).write_result()


def lock_fault(
    path: Path, *, context: Context, selector: Selector, expected_state: str, reason: str
) -> WriteResult:
    return _execute(
        path,
        context,
        "fault",
        selector=selector,
        expected_state=expected_state,
        reason=reason,
    ).write_result()
