import json
import math
import os
import re
import sqlite3
from dataclasses import astuple, dataclass
from enum import StrEnum
from pathlib import Path, PosixPath
from typing import Any, Literal
from uuid import UUID

from agent_board.night import ledger_binding
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.ledger import _SCHEMA
from agent_board.night.ledger_binding import BindingClaim
from agent_board.night_state import StateError, _check_file, check_private_file

QUEUE_VERSION = 4
MAX_INT = 2**63 - 1
PAYLOAD_MAX = 8192
ACK_CLAIM_MAX = 512
FENCE = re.compile(r"[A-Za-z0-9_-]{32,128}")
EFFECTIVE_QUEUE_DDL = """CREATE TABLE effective_queue (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    generation TEXT NOT NULL CHECK(typeof(generation) = 'text' AND length(CAST(generation AS BLOB)) = 36),
    revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision BETWEEN 0 AND 9223372036854775807),
    active_seq INTEGER CHECK(active_seq IS NULL OR (typeof(active_seq) = 'integer' AND active_seq BETWEEN 1 AND 9223372036854775807))
)"""
OUTCOMES = ("empty", "uncertain")
REASONS = ("completed", "deadline", "off", "preempted", "recovery", "proof_incomplete", "guard_lost")
_BINDING_FIELDS = frozenset(
    ("fence", "journal_id", "counter", "receipt_id", "boot_id", "invocation_id", "cgroup")
)
_EVIDENCE_FIELDS = _BINDING_FIELDS | {"outcome", "runtime_seconds", "reason"}
_UNSUPPORTED_ACTIONS = ("bootstrap", "capture", "capture_abort")


class Refusal(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    SIDECAR_PRESENT = "SIDECAR_PRESENT"
    BUSY = "BUSY"
    SCHEMA_REFUSED = "SCHEMA_REFUSED"
    ROW_REFUSED = "ROW_REFUSED"
    LEDGER_IDENTITY_MISMATCH = "LEDGER_IDENTITY_MISMATCH"
    CONTROL_MISMATCH = "CONTROL_MISMATCH"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    PUBLICATION_CONFLICT = "PUBLICATION_CONFLICT"
    RECEIPT_CONFLICT = "RECEIPT_CONFLICT"
    BINDING_CONFLICT = "BINDING_CONFLICT"
    BINDING_ALREADY_ACKED = "BINDING_ALREADY_ACKED"
    BINDING_NOT_ACKED = "BINDING_NOT_ACKED"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    NOTHING_SELECTED = "NOTHING_SELECTED"
    ACK_REFUSED = "ACK_REFUSED"
    EXHAUSTED = "EXHAUSTED"


class Fault(StrEnum):
    FILE_FAILURE = "FILE_FAILURE"
    SQLITE_FAILURE = "SQLITE_FAILURE"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"
    CLOSE_FAILURE = "CLOSE_FAILURE"
    SNAPSHOT_FAILURE = "SNAPSHOT_FAILURE"


@dataclass(frozen=True, slots=True)
class ExpectedControl:
    journal_id: str
    generation: str
    revision: int
    active_seq: int | None
    acked_counter: int


@dataclass(frozen=True, slots=True)
class BindingPublication:
    receipt_id: str
    run_id: str
    fence: str
    claim: BindingClaim


@dataclass(frozen=True, slots=True)
class EvidencePublication:
    receipt_id: str
    run_id: str
    fence: str
    claim: BindingClaim
    outcome: str
    reason: str
    runtime_seconds: int | float | None


@dataclass(frozen=True, slots=True)
class OrdinaryAckClaim:
    receipt_id: str
    journal_id: str
    counter: int
    state: str


@dataclass(frozen=True, slots=True)
class SelectedReceipt:
    seq: int
    receipt_id: str
    action: Literal["binding", "evidence"]
    run_id: str
    payload: str


@dataclass(frozen=True, slots=True)
class QueueResult:
    mutation: MutationOutcome
    cleanup: CleanupOutcome
    changed: bool | None
    control: ExpectedControl | None
    selected: SelectedReceipt | None
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
    control: ExpectedControl | None = None
    selected: SelectedReceipt | None = None

    def failed(self, fault: Fault) -> None:
        priority = {Fault.CLOSE_FAILURE: 3, Fault.ROLLBACK_FAILURE: 2}
        if self.fault is None or priority.get(fault, 1) > priority.get(self.fault, 1):
            self.fault = fault

    def result(self) -> QueueResult:
        usable = (
            self.finished
            and self.cleanup == CleanupOutcome.CLOSED
            and self.refusal is None
            and self.fault is None
            and self.mutation in (MutationOutcome.COMMITTED, MutationOutcome.ROLLED_BACK)
        )
        changed = self.changed if self.mutation == MutationOutcome.COMMITTED else False
        return QueueResult(
            self.mutation,
            self.cleanup,
            None if self.mutation == MutationOutcome.UNKNOWN else changed,
            self.control if usable else None,
            self.selected if usable else None,
            self.refusal,
            self.fault,
        )


def _require(condition: bool, refusal: Refusal = Refusal.ROW_REFUSED) -> None:
    if not condition:
        raise _Refused(refusal)


def _uuid(value: object, refusal: Refusal = Refusal.ROW_REFUSED, *, version4: bool = False) -> str:
    if type(value) is not str or len(value) != 36:
        raise _Refused(refusal)
    try:
        parsed = UUID(value)
    except ValueError:
        raise _Refused(refusal) from None
    _require(str(parsed) == value and (not version4 or parsed.version == 4), refusal)
    return value


def _integer(value: object, low: int, refusal: Refusal = Refusal.ROW_REFUSED) -> int:
    if type(value) is not int or not low <= value <= MAX_INT:
        raise _Refused(refusal)
    return value


def _fence(value: object, refusal: Refusal = Refusal.ROW_REFUSED) -> str:
    if type(value) is not str or FENCE.fullmatch(value) is None:
        raise _Refused(refusal)
    return value


def _runtime(value: object, refusal: Refusal = Refusal.ROW_REFUSED) -> int | float | None:
    if value is None:
        return None
    if type(value) is int:
        _require(0 <= value <= MAX_INT, refusal)
        return value
    if type(value) is float:
        _require(math.isfinite(value) and value >= 0, refusal)
        return value
    raise _Refused(refusal)


def _claim(value: object, refusal: Refusal) -> BindingClaim:
    if type(value) is not BindingClaim:
        raise _Refused(refusal)
    try:
        return ledger_binding._claim(value)
    except (ledger_binding._Refused, AttributeError, TypeError, ValueError):
        raise _Refused(refusal) from None


def _control(value: object) -> ExpectedControl:
    bad = Refusal.INVALID_INPUT
    if type(value) is not ExpectedControl:
        raise _Refused(bad)
    active = value.active_seq
    return ExpectedControl(
        _uuid(value.journal_id, bad),
        _uuid(value.generation, bad, version4=True),
        _integer(value.revision, 0, bad),
        None if active is None else _integer(active, 1, bad),
        _integer(value.acked_counter, 0, bad),
    )


def _publication(value: object) -> BindingPublication | EvidencePublication:
    bad = Refusal.INVALID_INPUT
    if type(value) is BindingPublication:
        claim = _claim(value.claim, bad)
        publication: BindingPublication | EvidencePublication = BindingPublication(
            _uuid(value.receipt_id, bad), _uuid(value.run_id, bad), _fence(value.fence, bad), claim
        )
    elif type(value) is EvidencePublication:
        claim = _claim(value.claim, bad)
        _require(type(value.outcome) is str and value.outcome in OUTCOMES, bad)
        _require(type(value.reason) is str and value.reason in REASONS, bad)
        publication = EvidencePublication(
            _uuid(value.receipt_id, bad),
            _uuid(value.run_id, bad),
            _fence(value.fence, bad),
            claim,
            value.outcome,
            value.reason,
            _runtime(value.runtime_seconds, bad),
        )
    else:
        raise _Refused(bad)
    _require(publication.claim.run_id == publication.run_id, bad)
    return publication


def _ack(value: object) -> OrdinaryAckClaim:
    bad = Refusal.INVALID_INPUT
    if type(value) is not OrdinaryAckClaim:
        raise _Refused(bad)
    _require(type(value.state) is str and value.state in ("running", "finished", "quarantined"), bad)
    claim = OrdinaryAckClaim(
        _uuid(value.receipt_id, bad),
        _uuid(value.journal_id, bad),
        _integer(value.counter, 1, bad),
        value.state,
    )
    _require(
        len(
            _canonical(
                dict(zip(("receipt_id", "journal_id", "counter", "state"), astuple(claim), strict=True))
            )
        )
        <= ACK_CLAIM_MAX,
        bad,
    )
    return claim


def _path(value: object) -> Path:
    if type(value) is not PosixPath:
        raise _Refused(Refusal.INVALID_INPUT)
    return value


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _action(publication: BindingPublication | EvidencePublication) -> Literal["binding", "evidence"]:
    return "binding" if type(publication) is BindingPublication else "evidence"


def _payload(publication: BindingPublication | EvidencePublication, expected: ExpectedControl) -> str:
    claim = publication.claim
    payload: dict[str, object] = {
        "fence": publication.fence,
        "journal_id": expected.journal_id,
        "counter": expected.acked_counter,
        "receipt_id": publication.receipt_id,
        "boot_id": claim.boot_id,
        "invocation_id": claim.invocation_id,
        "cgroup": claim.cgroup,
    }
    if type(publication) is EvidencePublication:
        payload |= {
            "outcome": publication.outcome,
            "runtime_seconds": publication.runtime_seconds,
            "reason": publication.reason,
        }
    text = _canonical(payload)
    _require(len(text) <= PAYLOAD_MAX, Refusal.INVALID_INPUT)
    return text


_IDENTITY = {"singleton": ("integer", 0), "journal_id": ("text", 36), "acked_counter": ("integer", 0)}
_CONTROL = {
    "singleton": ("integer", 0),
    "generation": ("text", 36),
    "revision": ("integer", 0),
    "active_seq": ("integer", 0),
}
_RECEIPT = {
    "seq": ("integer", 0),
    "receipt_id": ("text", 36),
    "action": ("text", 16),
    "run_id": ("text", 36),
    "payload": ("text", PAYLOAD_MAX),
    "acked": ("integer", 0),
}
_BINDING = {
    "run_id": ("text", 36),
    "uid": ("integer", 0),
    "boot_id": ("text", 36),
    "invocation_id": ("text", 32),
    "cgroup": ("text", 4096),
    "server_acked": ("integer", 0),
}
_SEQUENCE = {"name": ("text", 128), "seq": ("integer", 0)}
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


CATALOG_QUERY = f"SELECT {_projection(_CATALOG)} FROM sqlite_schema LIMIT 18"
IDENTITY_QUERY = f"SELECT {_projection(_IDENTITY)} FROM identity LIMIT 2"
CONTROL_QUERY = f"SELECT {_projection(_CONTROL)} FROM effective_queue LIMIT 2"
SELECTED_QUERY = f"SELECT {_projection(_RECEIPT)} FROM receipts WHERE seq=? LIMIT 2"
RECEIPT_ID_QUERY = (
    "SELECT seq FROM receipts INDEXED BY sqlite_autoindex_receipts_1 WHERE receipt_id=? LIMIT 2"
)
EVIDENCE_QUERY = (
    "SELECT seq FROM receipts INDEXED BY one_evidence_per_run WHERE run_id=? AND action='evidence' LIMIT 2"
)
BINDING_QUERY = f"SELECT {_projection(_BINDING)} FROM bindings INDEXED BY sqlite_autoindex_bindings_1 WHERE run_id=? LIMIT 2"
PARENT_QUERY = "SELECT 1 FROM capture_aborts INDEXED BY sqlite_autoindex_capture_aborts_1 WHERE parent_receipt_id=? LIMIT 2"
SEQUENCE_QUERY = f"SELECT {_projection(_SEQUENCE)} FROM sqlite_sequence LIMIT 2"
GREATEST_QUERY = "SELECT seq FROM receipts ORDER BY seq DESC LIMIT 1"


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
            _require(type(value) is int)
            result[name] = value
    return result


def _sql(statement: str) -> str:
    return " ".join(re.sub(r"\bIF NOT EXISTS\b", "", statement).split())


def _expected_catalog() -> dict[str, tuple[str, str, str | None]]:
    objects: dict[str, tuple[str, str, str | None]] = {}
    for statement in _SCHEMA:
        match = re.match(r"CREATE (?:UNIQUE )?(TABLE|INDEX) IF NOT EXISTS (\w+)", statement)
        if match is not None:
            name, kind = match[2], match[1].lower()
            table = name if kind == "table" else statement.split(" ON ", 1)[1].split()[0]
            objects[name] = kind, table, _sql(statement)
    for table, count in (
        ("receipts", 1),
        ("capture_aborts", 2),
        ("captures", 2),
        ("bindings", 1),
        ("arms", 1),
    ):
        for index in range(1, count + 1):
            objects[f"sqlite_autoindex_{table}_{index}"] = "index", table, None
    objects["sqlite_sequence"] = "table", "sqlite_sequence", "CREATE TABLE sqlite_sequence(name,seq)"
    objects["effective_queue"] = "table", "effective_queue", _sql(EFFECTIVE_QUEUE_DDL)
    return objects


_EXPECTED_CATALOG = _expected_catalog()


def _validate(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise _Failed(Fault.SNAPSHOT_FAILURE)
    for statement, expected in (("PRAGMA journal_mode", "delete"), ("PRAGMA user_version", QUEUE_VERSION)):
        rows = conn.execute(statement).fetchall()
        _require(
            len(rows) == 1 and type(rows[0][0]) is type(expected) and rows[0][0] == expected,
            Refusal.SCHEMA_REFUSED,
        )
    rows = conn.execute(CATALOG_QUERY).fetchall()
    _require(len(rows) == len(_EXPECTED_CATALOG), Refusal.SCHEMA_REFUSED)
    actual: dict[str, tuple[object, object, object]] = {}
    try:
        for raw in rows:
            row = _row(raw, _CATALOG, nullable=frozenset(("sql",)))
            name, statement = row["name"], row["sql"]
            _require(isinstance(name, str) and name not in actual)
            actual[name] = (
                row["type"],
                row["tbl_name"],
                _sql(statement) if isinstance(statement, str) else None,
            )
    except _Refused:
        raise _Refused(Refusal.SCHEMA_REFUSED) from None
    _require(actual == _EXPECTED_CATALOG, Refusal.SCHEMA_REFUSED)


def _read_control(conn: sqlite3.Connection) -> ExpectedControl:
    rows = conn.execute(IDENTITY_QUERY).fetchall()
    _require(len(rows) == 1)
    identity = _row(rows[0], _IDENTITY, nullable=frozenset(("journal_id",)))
    _require(identity["singleton"] == 1 and identity["journal_id"] is not None)
    rows = conn.execute(CONTROL_QUERY).fetchall()
    _require(len(rows) == 1)
    control = _row(rows[0], _CONTROL, nullable=frozenset(("active_seq",)))
    _require(control["singleton"] == 1)
    active = control["active_seq"]
    return ExpectedControl(
        _uuid(identity["journal_id"]),
        _uuid(control["generation"], version4=True),
        _integer(control["revision"], 0),
        None if active is None else _integer(active, 1),
        _integer(identity["acked_counter"], 0),
    )


def _check_control(conn: sqlite3.Connection, expected: ExpectedControl) -> None:
    stored = _read_control(conn)
    _require(stored.journal_id == expected.journal_id, Refusal.LEDGER_IDENTITY_MISMATCH)
    _require(stored == expected, Refusal.CONTROL_MISMATCH)


def _strict_json(text: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in items]
        _require(len(keys) == len(set(keys)))
        return dict(items)

    def constant(name: str) -> None:
        raise _Refused(Refusal.ROW_REFUSED)

    try:
        value: object = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        raise _Refused(Refusal.ROW_REFUSED) from None
    if type(value) is not dict:
        raise _Refused(Refusal.ROW_REFUSED)
    return value


def _binding(conn: sqlite3.Connection, run_id: str) -> tuple[BindingClaim, bool] | None:
    rows = conn.execute(BINDING_QUERY, (run_id,)).fetchall()
    _require(len(rows) <= 1)
    if not rows:
        return None
    values = _row(rows[0], _BINDING)
    acked = values.pop("server_acked")
    _require(acked in (0, 1))
    claim = _claim(BindingClaim(**values), Refusal.ROW_REFUSED)
    _require(claim.run_id == run_id)
    return claim, bool(acked)


def _selected(conn: sqlite3.Connection, control: ExpectedControl) -> tuple[SelectedReceipt, BindingClaim]:
    seq = control.active_seq
    if seq is None:
        raise _Refused(Refusal.NOTHING_SELECTED)
    rows = conn.execute(SELECTED_QUERY, (seq,)).fetchall()
    _require(len(rows) == 1)
    row = _row(rows[0], _RECEIPT)
    action = row["action"]
    _require(action not in _UNSUPPORTED_ACTIONS, Refusal.UNSUPPORTED_ACTION)
    _require(action in ("binding", "evidence") and row["seq"] == seq and row["acked"] == 0)
    receipt_id, run_id, text = _uuid(row["receipt_id"]), _uuid(row["run_id"]), row["payload"]
    _require(conn.execute(PARENT_QUERY, (receipt_id,)).fetchall() == [])
    payload = _strict_json(text)
    _require(set(payload) == (_BINDING_FIELDS if action == "binding" else _EVIDENCE_FIELDS))
    _require(_canonical(payload) == text)
    _fence(payload["fence"])
    _require(payload["journal_id"] == control.journal_id and payload["receipt_id"] == receipt_id)
    _require(type(payload["counter"]) is int and payload["counter"] == control.acked_counter)
    if action == "evidence":
        _require(payload["outcome"] in OUTCOMES and payload["reason"] in REASONS)
        _runtime(payload["runtime_seconds"])
    stored = _binding(conn, run_id)
    if stored is None:
        raise _Refused(Refusal.ROW_REFUSED)
    claim, acked = stored
    _require(acked is (action == "evidence"))
    _require(
        (payload["boot_id"], payload["invocation_id"], payload["cgroup"])
        == (claim.boot_id, claim.invocation_id, claim.cgroup)
    )
    return SelectedReceipt(seq, receipt_id, action, run_id, text), claim


def _next_seq(conn: sqlite3.Connection) -> int:
    rows = conn.execute(SEQUENCE_QUERY).fetchall()
    _require(len(rows) <= 1)
    allocated = 0
    if rows:
        row = _row(rows[0], _SEQUENCE)
        _require(row["name"] == "receipts")
        allocated = _integer(row["seq"], 0)
    greatest = conn.execute(GREATEST_QUERY).fetchall()
    if greatest:
        top = greatest[0][0]
        _require(type(top) is int and 1 <= top <= allocated)
    _require(allocated < MAX_INT, Refusal.EXHAUSTED)
    return allocated + 1


def _publish(
    conn: sqlite3.Connection,
    attempt: _Attempt,
    expected: ExpectedControl,
    publication: BindingPublication | EvidencePublication,
) -> None:
    action, payload = _action(publication), _payload(publication, expected)
    if expected.active_seq is not None:
        selected, claim = _selected(conn, expected)
        _require(
            (selected.receipt_id, selected.action, selected.run_id, selected.payload, claim)
            == (publication.receipt_id, action, publication.run_id, payload, publication.claim),
            Refusal.PUBLICATION_CONFLICT,
        )
        attempt.control, attempt.selected = expected, selected
        _rollback(conn, attempt)
        return
    _require(expected.revision < MAX_INT and expected.acked_counter < MAX_INT, Refusal.EXHAUSTED)
    stored = _binding(conn, publication.run_id)
    if stored is not None:
        _require(stored[0] == publication.claim, Refusal.BINDING_CONFLICT)
    if action == "binding":
        _require(stored is None or not stored[1], Refusal.BINDING_ALREADY_ACKED)
    else:
        _require(stored is not None and stored[1], Refusal.BINDING_NOT_ACKED)
        _require(
            conn.execute(EVIDENCE_QUERY, (publication.run_id,)).fetchall() == [], Refusal.EVIDENCE_CONFLICT
        )
    _require(
        conn.execute(RECEIPT_ID_QUERY, (publication.receipt_id,)).fetchall() == [], Refusal.RECEIPT_CONFLICT
    )
    seq = _next_seq(conn)
    attempt.changed = True
    if stored is None:
        _write(
            conn,
            "INSERT INTO bindings(run_id,uid,boot_id,invocation_id,cgroup,server_acked) VALUES(?,?,?,?,?,0)",
            astuple(publication.claim),
        )
    cursor = _write(
        conn,
        "INSERT INTO receipts(receipt_id,action,run_id,payload,acked) VALUES(?,?,?,?,0)",
        (publication.receipt_id, action, publication.run_id, payload),
    )
    if cursor.lastrowid != seq:
        raise _Failed(Fault.SNAPSHOT_FAILURE)
    _write(
        conn,
        "UPDATE effective_queue SET revision=?,active_seq=? WHERE singleton=1 AND revision=? AND active_seq IS NULL",
        (expected.revision + 1, seq, expected.revision),
    )
    after = ExpectedControl(
        expected.journal_id, expected.generation, expected.revision + 1, seq, expected.acked_counter
    )
    selected, claim = _after(conn, after)
    if (selected.receipt_id, selected.action, selected.run_id, selected.payload, claim) != (
        publication.receipt_id,
        action,
        publication.run_id,
        payload,
        publication.claim,
    ):
        raise _Failed(Fault.SNAPSHOT_FAILURE)
    _commit(conn, attempt, after, selected)


def _acknowledge(
    conn: sqlite3.Connection,
    attempt: _Attempt,
    expected: ExpectedControl,
    ack: OrdinaryAckClaim,
) -> None:
    _require(expected.active_seq is not None, Refusal.NOTHING_SELECTED)
    selected, claim = _selected(conn, expected)
    outcome = _strict_json(selected.payload).get("outcome")
    states = {
        "binding": ("running",),
        "evidence": ("finished",) if outcome == "empty" else ("finished", "quarantined"),
    }
    _require(expected.acked_counter < MAX_INT and expected.revision < MAX_INT, Refusal.EXHAUSTED)
    _require(
        ack.receipt_id == selected.receipt_id
        and ack.journal_id == expected.journal_id
        and ack.counter == expected.acked_counter + 1
        and ack.state in states[selected.action],
        Refusal.ACK_REFUSED,
    )
    attempt.changed = True
    _write(conn, "UPDATE receipts SET acked=1 WHERE seq=? AND acked=0", (selected.seq,))
    if selected.action == "binding":
        _write(
            conn, "UPDATE bindings SET server_acked=1 WHERE run_id=? AND server_acked=0", (selected.run_id,)
        )
    _write(
        conn,
        "UPDATE identity SET acked_counter=? WHERE singleton=1 AND acked_counter=?",
        (ack.counter, expected.acked_counter),
    )
    _write(
        conn,
        "UPDATE effective_queue SET revision=?,active_seq=NULL WHERE singleton=1 AND revision=? AND active_seq=?",
        (expected.revision + 1, expected.revision, selected.seq),
    )
    after = ExpectedControl(
        expected.journal_id, expected.generation, expected.revision + 1, None, ack.counter
    )
    try:
        if _read_control(conn) != after:
            raise _Failed(Fault.SNAPSHOT_FAILURE)
        rows = conn.execute(SELECTED_QUERY, (selected.seq,)).fetchall()
        _require(len(rows) == 1)
        row = _row(rows[0], _RECEIPT)
        _require(
            (row["seq"], row["receipt_id"], row["action"], row["run_id"], row["payload"], row["acked"])
            == (selected.seq, selected.receipt_id, selected.action, selected.run_id, selected.payload, 1)
        )
        _require(_binding(conn, selected.run_id) == (claim, True))
    except _Refused:
        raise _Failed(Fault.SNAPSHOT_FAILURE) from None
    _commit(conn, attempt, after, None)


def _after(conn: sqlite3.Connection, after: ExpectedControl) -> tuple[SelectedReceipt, BindingClaim]:
    try:
        if _read_control(conn) != after:
            raise _Failed(Fault.SNAPSHOT_FAILURE)
        return _selected(conn, after)
    except _Refused:
        raise _Failed(Fault.SNAPSHOT_FAILURE) from None


def _write(conn: sqlite3.Connection, statement: str, parameters: tuple[object, ...]) -> sqlite3.Cursor:
    cursor = conn.execute(statement, parameters)
    if cursor.rowcount != 1:
        raise _Failed(Fault.SNAPSHOT_FAILURE)
    return cursor


def _commit(
    conn: sqlite3.Connection,
    attempt: _Attempt,
    after: ExpectedControl,
    selected: SelectedReceipt | None,
) -> None:
    attempt.control, attempt.selected = after, selected
    attempt.commit_attempted = True
    conn.execute("COMMIT")
    attempt.mutation = MutationOutcome.COMMITTED


def _close_fd(fd: int, attempt: _Attempt) -> None:
    try:
        os.close(fd)
    except OSError:
        attempt.cleanup = CleanupOutcome.UNCERTAIN
        attempt.failed(Fault.CLOSE_FAILURE)
        raise


def _existing(path: Path, attempt: _Attempt) -> tuple[int, int]:
    if not path.is_absolute() or path.anchor != "/" or path != Path(os.path.normpath(path)):
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


def _run(path: object, expected: object, operation: str, argument: object) -> QueueResult:
    attempt = _Attempt()
    conn: sqlite3.Connection | None = None
    try:
        try:
            checked_path, control = _path(path), _control(expected)
            publication = _publication(argument) if operation == "publish" else None
            ack = _ack(argument) if operation == "acknowledge" else None
        except AttributeError:
            raise _Refused(Refusal.INVALID_INPUT) from None
        identity = _existing(checked_path, attempt)
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
        _validate(conn)
        _check_control(conn, control)
        if publication is not None:
            _publish(conn, attempt, control, publication)
        elif ack is not None:
            _acknowledge(conn, attempt, control, ack)
        else:
            attempt.control = control
            attempt.selected = None if control.active_seq is None else _selected(conn, control)[0]
            _rollback(conn, attempt)
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


def observe_ordinary(path: Path, *, expected: ExpectedControl) -> QueueResult:
    return _run(path, expected, "observe", None)


def publish_ordinary(
    path: Path,
    *,
    expected: ExpectedControl,
    publication: BindingPublication | EvidencePublication,
) -> QueueResult:
    return _run(path, expected, "publish", publication)


def acknowledge_ordinary(path: Path, *, expected: ExpectedControl, ack: OrdinaryAckClaim) -> QueueResult:
    return _run(path, expected, "acknowledge", ack)
