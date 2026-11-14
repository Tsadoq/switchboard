import sqlite3
from dataclasses import dataclass
from typing import Any

from agent_board.night.fixture_gate_dto import (
    BODY_CAP,
    BootstrapSelector,
    Context,
    FixtureBootstrap,
    InvalidRecord,
    NormalAttestation,
    RecordBody,
    RuntimeOwner,
    RuntimeSelector,
    Selector,
    decode_record,
)

DDL = (
    """CREATE TABLE control (
 id INTEGER PRIMARY KEY CHECK(id=1),
 store_version INTEGER NOT NULL, policy_version INTEGER NOT NULL,
 generation TEXT NOT NULL, origin TEXT NOT NULL, supervisor_release_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('IDLE','PENDING','FAULT_LOCKED')),
 operation TEXT NOT NULL, launch TEXT NOT NULL,
 completion_ref TEXT REFERENCES completions(ref), fault_reason TEXT
)""",
    """CREATE TABLE identities (
 operation TEXT NOT NULL PRIMARY KEY, launch TEXT NOT NULL UNIQUE,
 store_version INTEGER NOT NULL, policy_version INTEGER NOT NULL,
 generation TEXT NOT NULL, origin TEXT NOT NULL, supervisor_release_sha256 TEXT NOT NULL,
 run_id TEXT, fence_sha256 TEXT,
 status TEXT NOT NULL CHECK(status IN ('BOOTSTRAP','PENDING','COMPLETED','FAULTED'))
)""",
    """CREATE TABLE completions (
 ref TEXT NOT NULL PRIMARY KEY,
 operation TEXT NOT NULL UNIQUE REFERENCES identities(operation), launch TEXT NOT NULL UNIQUE,
 store_version INTEGER NOT NULL, policy_version INTEGER NOT NULL,
 generation TEXT NOT NULL, origin TEXT NOT NULL, supervisor_release_sha256 TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('FIXTURE_BOOTSTRAP_V1','NORMAL_ATTESTATION_V1')),
 body BLOB NOT NULL
)""",
)

CONTEXT_COLUMNS = {
    "store_version": 0,
    "policy_version": 0,
    "generation": 36,
    "origin": 64,
    "supervisor_release_sha256": 64,
}
CONTROL_COLUMNS = {
    "id": 0,
    **CONTEXT_COLUMNS,
    "state": 64,
    "operation": 36,
    "launch": 36,
    "completion_ref": 181,
    "fault_reason": 64,
}
IDENTITY_COLUMNS = {
    "operation": 36,
    "launch": 36,
    **CONTEXT_COLUMNS,
    "run_id": 36,
    "fence_sha256": 64,
    "status": 64,
}
COMPLETION_COLUMNS = {
    "ref": 181,
    "operation": 36,
    "launch": 36,
    **CONTEXT_COLUMNS,
    "kind": 64,
    "body": BODY_CAP,
}
CATALOG_COLUMNS = {"type": 16, "name": 64, "tbl_name": 64, "sql": 16384}
FAULT_REASONS = frozenset(
    (
        "RESOURCE_UNCERTAIN",
        "PROTOCOL_REFUSED",
        "PUBLICATION_UNCERTAIN",
        "LIFECYCLE_UNCERTAIN",
        "OPERATOR_HOLD",
    )
)


class StoreRefusal(ValueError):
    def __init__(self, reason: str = "SCHEMA_REFUSED") -> None:
        super().__init__(reason)
        self.reason = reason


def _require(condition: bool, reason: str = "SCHEMA_REFUSED") -> None:
    if not condition:
        raise StoreRefusal(reason)


def _projection(columns: dict[str, int]) -> str:
    fields: list[str] = []
    for name, cap in columns.items():
        value = (
            f"substr(CAST({name} AS BLOB),1,{cap + 1})"
            if cap
            else f"CASE WHEN typeof({name})='integer' THEN {name} END"
        )
        fields.extend((f"{value} AS {name}", f"typeof({name}) AS {name}_kind"))
    return ",".join(fields)


CONTROL_QUERY = f"SELECT {_projection(CONTROL_COLUMNS)} FROM control LIMIT 2"
IDENTITY_QUERY = (
    f"SELECT {_projection(IDENTITY_COLUMNS)} FROM identities "
    "INDEXED BY sqlite_autoindex_identities_1 WHERE operation=? LIMIT 2"
)
COMPLETION_OPERATION_QUERY = (
    f"SELECT {_projection(COMPLETION_COLUMNS)} FROM completions "
    "INDEXED BY sqlite_autoindex_completions_2 WHERE operation=? LIMIT 2"
)
COMPLETION_LAUNCH_QUERY = (
    f"SELECT {_projection(COMPLETION_COLUMNS)} FROM completions "
    "INDEXED BY sqlite_autoindex_completions_3 WHERE launch=? LIMIT 2"
)
CATALOG_QUERY = f"SELECT {_projection(CATALOG_COLUMNS)} FROM sqlite_schema LIMIT 9"


@dataclass(frozen=True)
class Observation:
    classification: str
    context: Context
    selector: Selector
    completion: RecordBody | None
    fault_reason: str | None


@dataclass(frozen=True)
class _Current:
    observation: Observation
    control: dict[str, Any]
    identity: dict[str, Any]
    completion: dict[str, Any] | None


def _row(
    row: sqlite3.Row,
    columns: dict[str, int],
    *,
    nullable: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, cap in columns.items():
        kind, value = row[f"{name}_kind"], row[name]
        if kind == "null" and name in nullable:
            result[name] = None
        elif cap == 0:
            _require(kind == "integer" and type(value) is int)
            result[name] = value
        else:
            expected = "blob" if name == "body" else "text"
            _require(kind == expected and type(value) is bytes and len(value) <= cap)
            try:
                result[name] = value if name == "body" else value.decode("utf-8")
            except UnicodeError as exc:
                raise StoreRefusal() from exc
    return result


def _normal_sql(value: str) -> str:
    return " ".join(value.rstrip(";").split())


def _validate(conn: sqlite3.Connection) -> None:
    _require(conn.in_transaction, "SNAPSHOT_FAILURE")
    for statement, expected in (
        ("PRAGMA journal_mode", "delete"),
        ("PRAGMA user_version", 1),
        ("PRAGMA foreign_keys", 1),
    ):
        rows = conn.execute(statement).fetchall()
        _require(len(rows) == 1 and type(rows[0][0]) is type(expected) and rows[0][0] == expected)
    expected_objects: dict[str, tuple[str, str, str | None]] = {
        name: ("table", name, _normal_sql(ddl))
        for name, ddl in zip(("control", "identities", "completions"), DDL, strict=True)
    }
    for table, count in (("identities", 2), ("completions", 3)):
        for index in range(1, count + 1):
            expected_objects[f"sqlite_autoindex_{table}_{index}"] = ("index", table, None)
    rows = conn.execute(CATALOG_QUERY).fetchall()
    _require(len(rows) == len(expected_objects))
    found: dict[str, tuple[str, str, str | None]] = {}
    for raw in rows:
        row = _row(raw, CATALOG_COLUMNS, nullable=frozenset(("sql",)))
        _require(row["name"] not in found)
        sql = None if row["sql"] is None else _normal_sql(row["sql"])
        found[row["name"]] = (row["type"], row["tbl_name"], sql)
    _require(found == expected_objects)


def _context(row: dict[str, Any], expected: Context) -> None:
    try:
        actual = Context(**{name: row[name] for name in CONTEXT_COLUMNS})
    except InvalidRecord as exc:
        raise StoreRefusal() from exc
    _require(actual == expected, "CONTEXT_MISMATCH")


def _completion_rows(rows: list[sqlite3.Row], context: Context) -> dict[str, Any] | None:
    _require(len(rows) <= 1)
    if not rows:
        return None
    result = _row(rows[0], COMPLETION_COLUMNS)
    _context(result, context)
    return result


def _current(conn: sqlite3.Connection, context: Context) -> _Current:
    _require(conn.in_transaction, "SNAPSHOT_FAILURE")
    rows = conn.execute(CONTROL_QUERY).fetchall()
    _require(len(rows) == 1)
    control = _row(rows[0], CONTROL_COLUMNS, nullable=frozenset(("completion_ref", "fault_reason")))
    _require(control["id"] == 1)
    _context(control, context)
    rows = conn.execute(IDENTITY_QUERY, (control["operation"],)).fetchall()
    _require(len(rows) == 1)
    identity = _row(rows[0], IDENTITY_COLUMNS, nullable=frozenset(("run_id", "fence_sha256")))
    _context(identity, context)
    _require(identity["operation"] == control["operation"] and identity["launch"] == control["launch"])
    by_operation = _completion_rows(
        conn.execute(COMPLETION_OPERATION_QUERY, (control["operation"],)).fetchall(), context
    )
    by_launch = _completion_rows(
        conn.execute(COMPLETION_LAUNCH_QUERY, (control["launch"],)).fetchall(), context
    )
    _require(by_operation == by_launch, "ATTESTATION_REFUSED")
    status, state = identity["status"], control["state"]
    _require(status in ("BOOTSTRAP", "COMPLETED", "PENDING", "FAULTED"))
    selector: Selector
    try:
        if status == "BOOTSTRAP":
            selector = BootstrapSelector(
                operation=identity["operation"],
                launch=identity["launch"],
                run_id=identity["run_id"],
                fence_sha256=identity["fence_sha256"],
            )
        else:
            selector = RuntimeSelector(
                RuntimeOwner(
                    **{name: identity[name] for name in ("operation", "launch", "run_id", "fence_sha256")}
                )
            )
    except InvalidRecord as exc:
        raise StoreRefusal() from exc
    completion: RecordBody | None = None
    if status in ("BOOTSTRAP", "COMPLETED"):
        _require(by_operation is not None, "ATTESTATION_REFUSED")
        if by_operation is None:
            raise StoreRefusal("ATTESTATION_REFUSED")
        reference = context.reference(identity["operation"], identity["launch"])
        _require(by_operation["ref"] == reference, "ATTESTATION_REFUSED")
        _require(by_operation["operation"] == identity["operation"], "ATTESTATION_REFUSED")
        _require(by_operation["launch"] == identity["launch"], "ATTESTATION_REFUSED")
        try:
            completion = decode_record(
                by_operation["body"],
                context=context,
                owner=selector.owner if isinstance(selector, RuntimeSelector) else None,
            )
        except InvalidRecord as exc:
            raise StoreRefusal("ATTESTATION_REFUSED") from exc
        expected_type = FixtureBootstrap if status == "BOOTSTRAP" else NormalAttestation
        _require(type(completion) is expected_type, "ATTESTATION_REFUSED")
        _require(completion.kind == by_operation["kind"], "ATTESTATION_REFUSED")
    else:
        _require(by_operation is None, "ATTESTATION_REFUSED")
    if state == "IDLE":
        _require(status in ("BOOTSTRAP", "COMPLETED"))
        _require(by_operation is not None and control["completion_ref"] == by_operation["ref"])
        _require(control["fault_reason"] is None)
        classification = (
            "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP"
            if status == "BOOTSTRAP"
            else "IDLE_WITH_STRUCTURALLY_VALID_ATTESTATION"
        )
    elif state == "PENDING":
        _require(status == "PENDING" and control["completion_ref"] is None)
        _require(control["fault_reason"] is None)
        classification = "PENDING"
    else:
        _require(state == "FAULT_LOCKED" and status in ("BOOTSTRAP", "COMPLETED", "FAULTED"))
        _require(control["completion_ref"] is None and control["fault_reason"] in FAULT_REASONS)
        classification = "FAULT_LOCKED"
    observation = Observation(classification, context, selector, completion, control["fault_reason"])
    return _Current(observation, control, identity, by_operation)
