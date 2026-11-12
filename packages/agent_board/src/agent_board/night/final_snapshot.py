import json
import math
import re
import secrets
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_board.night.assignment import (
    MAX_ASSIGNMENT_BYTES,
    build_assignment,
    encode,
    parse_wakes,
    policy_from,
)
from agent_board.night.ledger import _SCHEMA, RecordedBinding
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import ClockReading, Grant, Record, StateError, check_private_file

MAX_JSON_BYTES = 131072
MAX_SCHEMA_ROWS = 32
MAX_SCHEMA_SQL_BYTES = 8192
MAX_COUNTER = 2**63 - 1
_JOURNAL_SCHEMA = (
    (
        "CREATE TABLE IF NOT EXISTS runs ("
        "run_id TEXT PRIMARY KEY, fence TEXT NOT NULL, principal TEXT NOT NULL, uid INTEGER NOT NULL, "
        "boot_id TEXT NOT NULL, deadline_boottime REAL NOT NULL, deadline_utc REAL NOT NULL, "
        "state TEXT NOT NULL CHECK(state IN ('reserved', 'starting', 'stopping', 'quarantined', 'stopped')), "
        "launch_claimed INTEGER NOT NULL DEFAULT 0 CHECK(launch_claimed IN (0, 1)), "
        "stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0, 1)), "
        "uncertain INTEGER NOT NULL DEFAULT 0 CHECK(uncertain IN (0, 1)), reason TEXT, "
        "slot INTEGER NOT NULL DEFAULT 1 CHECK(slot = 1))"
    ),
    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs (slot) WHERE state != 'stopped'",
)
_PAYLOAD_KEYS = frozenset(
    ("fence", "journal_id", "counter", "receipt_id", "boot_id", "invocation_id", "cgroup")
)


class SnapshotRefused(StateError):
    pass


@dataclass(frozen=True)
class LocalSnapshot:
    record: Record
    binding: RecordedBinding
    journal_id: str
    capture_id: str


def final_snapshot(
    journal_path: Path,
    ledger_path: Path,
    *,
    expected: Grant,
    now: ClockReading,
    grace: float,
) -> LocalSnapshot:
    """Read local eligibility within the trusted caller's already-established ordering interval.

    This neither establishes that interval nor witnesses current kernel/manager state or permits enqueue.
    Paths and expected grant must come from trusted root state, never a remote request.
    """
    if type(expected) is not Grant or type(now) is not ClockReading or not _finite(grace) or grace < 0:
        raise SnapshotRefused("invalid_snapshot_input")
    try:
        with (
            _database(journal_path, 2, _JOURNAL_SCHEMA, {"runs": 1}) as journal,
            _database(
                ledger_path,
                3,
                _SCHEMA,
                {"receipts": 1, "capture_aborts": 2, "captures": 2, "bindings": 1, "arms": 1},
            ) as ledger,
        ):
            record = _run(journal, expected, now, grace)
            identity = _point(
                ledger, "identity", "singleton", 1, {}, ("singleton", "acked_counter"), {"journal_id": 36}
            )
            journal_id = _uuid(identity["journal_id"])
            if identity["singleton"] != 1 or not _counter(identity["acked_counter"]):
                raise SnapshotRefused("identity_corrupt")
            bound = _point(
                ledger,
                "bindings",
                "run_id",
                record.run_id,
                {},
                ("uid", "server_acked"),
                {"run_id": 36, "boot_id": 36, "invocation_id": 32, "cgroup": 4096},
            )
            if (
                bound["run_id"] != record.run_id
                or bound["uid"] != record.uid
                or type(bound["uid"]) is not int
                or bound["boot_id"] != record.boot_id
                or type(bound["server_acked"]) is not int
                or bound["server_acked"] != 1
                or re.fullmatch(r"[0-9a-f]{32}", bound["invocation_id"]) is None
                or bound["cgroup"] != unit_cgroup(f"switchboard-night-{record.run_id}.service")
            ):
                raise SnapshotRefused("binding_mismatch")
            binding = RecordedBinding(
                record.run_id, record.uid, record.boot_id, bound["invocation_id"], bound["cgroup"], True
            )
            captured = _point(
                ledger,
                "captures",
                "run_id",
                record.run_id,
                {"assignment": MAX_ASSIGNMENT_BYTES},
                ("cutoff_utc",),
                {
                    "run_id": 36,
                    "receipt_id": 36,
                    "principal": 255,
                    "policy": 4096,
                    "snapshot": MAX_JSON_BYTES,
                },
            )
            capture_id = _uuid(captured["receipt_id"])
            receipt = _point(
                ledger,
                "receipts",
                "receipt_id",
                capture_id,
                {},
                ("seq", "acked"),
                {"receipt_id": 36, "action": 16, "run_id": 36, "payload": 8192},
            )
            if (
                ledger.execute(
                    "SELECT 1 FROM capture_aborts WHERE parent_receipt_id = ? LIMIT 1", (capture_id,)
                ).fetchone()
                is not None
            ):
                raise SnapshotRefused("capture_aborted")
            _capture(
                record, captured, receipt, journal_id, identity["acked_counter"], binding, now.utc, grace
            )
            return LocalSnapshot(record, binding, journal_id, capture_id)
    except SnapshotRefused:
        raise
    except (sqlite3.Error, StateError, ValueError, TypeError, OverflowError, RecursionError, OSError):
        raise SnapshotRefused("snapshot_unavailable_or_corrupt") from None


def _run(conn: sqlite3.Connection, expected: Grant, now: ClockReading, grace: float) -> Record:
    row = _point(
        conn,
        "runs",
        "run_id",
        expected.run_id,
        {},
        ("uid", "deadline_boottime", "deadline_utc", "launch_claimed", "stop_requested", "uncertain", "slot"),
        {"run_id": 36, "fence": 256, "principal": 255, "boot_id": 36, "state": 16, "reason": 256},
        nullable={"reason"},
    )
    active = conn.execute(
        "SELECT substr(CAST(run_id AS BLOB),1,37) FROM runs INDEXED BY one_active_run WHERE slot = 1 AND state != 'stopped' LIMIT 2"
    ).fetchall()
    if len(active) != 1 or active[0][0] != expected.run_id.encode("ascii"):
        raise SnapshotRefused("not_single_active_run")
    if (
        row["run_id"] != expected.run_id
        or not secrets.compare_digest(row["fence"], expected.fence)
        or row["principal"] != expected.principal
        or type(row["uid"]) is not int
        or row["uid"] != expected.uid
        or row["boot_id"] != expected.boot_id
        or row["boot_id"] != now.boot_id
        or row["deadline_boottime"] != expected.deadline_boottime
        or row["deadline_utc"] != expected.deadline_utc
        or not all(_finite(row[key]) and row[key] >= 0 for key in ("deadline_boottime", "deadline_utc"))
        or row["state"] != "starting"
        or row["reason"] is not None
        or any(
            type(row[key]) is not int or row[key] != value
            for key, value in (("launch_claimed", 1), ("stop_requested", 0), ("uncertain", 0), ("slot", 1))
        )
    ):
        raise SnapshotRefused("run_mismatch_or_ineligible")
    if now.boottime >= row["deadline_boottime"] - grace or now.utc >= row["deadline_utc"] - grace:
        raise SnapshotRefused("run_expired")
    return Record(
        **{key: row[key] for key in Grant.__dataclass_fields__},
        state="starting",
        launch_claimed=True,
        stop_requested=False,
        uncertain=False,
        reason=row["reason"],
    )


def _capture(
    record: Record,
    captured: Mapping[str, Any],
    receipt: Mapping[str, Any],
    journal_id: str,
    watermark: int,
    binding: RecordedBinding,
    utc: float,
    grace: float,
) -> None:
    if (
        captured["run_id"] != record.run_id
        or captured["principal"] != record.principal
        or receipt["receipt_id"] != captured["receipt_id"]
        or receipt["run_id"] != record.run_id
        or receipt["action"] != "capture"
        or type(receipt["acked"]) is not int
        or receipt["acked"] != 1
        or type(receipt["seq"]) is not int
        or receipt["seq"] <= 0
        or not _finite(captured["cutoff_utc"])
        or captured["cutoff_utc"] <= 0
    ):
        raise SnapshotRefused("capture_mismatch")
    payload = _json(receipt["payload"])
    snapshot = _json(captured["snapshot"])
    policy = policy_from(_json(captured["policy"]))
    if (
        frozenset(payload) != _PAYLOAD_KEYS
        or payload["fence"] != record.fence
        or payload["journal_id"] != journal_id
        or payload["receipt_id"] != captured["receipt_id"]
        or payload["boot_id"] != binding.boot_id
        or payload["invocation_id"] != binding.invocation_id
        or payload["cgroup"] != binding.cgroup
        or not _counter(payload["counter"])
        or payload["counter"] >= watermark
        or snapshot.get("journal_id") != journal_id
        or type(snapshot.get("counter")) is not int
        or snapshot["counter"] != payload["counter"] + 1
        or snapshot.get("run_id") != record.run_id
        or snapshot.get("capture_id") != captured["receipt_id"]
        or snapshot.get("principal") != record.principal
        or not _finite(snapshot.get("deadline"))
        or snapshot["deadline"] <= 0
    ):
        raise SnapshotRefused("capture_association_mismatch")
    assignment = build_assignment(
        run_id=record.run_id,
        principal=record.principal,
        wakes=parse_wakes(snapshot.get("wakes")),
        policy=policy,
        deadline_utc=min(snapshot["deadline"], captured["cutoff_utc"]),
    )
    if (
        assignment is None
        or assignment.deadline_utc > record.deadline_utc
        or encode(assignment) != captured["assignment"]
    ):
        raise SnapshotRefused("assignment_mismatch")
    if utc >= assignment.deadline_utc - grace:
        raise SnapshotRefused("capture_expired")


def _point(
    conn: sqlite3.Connection,
    table: str,
    key: str,
    value: object,
    blobs: Mapping[str, int],
    numbers: Sequence[str],
    texts: Mapping[str, int],
    *,
    nullable: set[str] | None = None,
) -> dict[str, Any]:
    fields = {
        **{name: ("blob", cap) for name, cap in blobs.items()},
        **{name: ("text", cap) for name, cap in texts.items()},
    }
    projections = [
        f"CASE WHEN typeof({name}) IN ('integer','real') THEN {name} END AS {name}, typeof({name}) AS {name}_kind"
        for name in numbers
    ]
    projections += [
        f"substr(CAST({name} AS BLOB), 1, {cap + 1}) AS {name}, typeof({name}) AS {name}_kind"
        for name, (_, cap) in fields.items()
    ]
    rows = conn.execute(
        f"SELECT {', '.join(projections)} FROM {table} WHERE {key} = ? LIMIT 2", (value,)
    ).fetchall()
    if len(rows) != 1:
        raise SnapshotRefused("missing_or_duplicate_row")
    row = rows[0]
    result = {name: row[name] for name in numbers}
    if any(row[f"{name}_kind"] not in ("integer", "real") for name in numbers):
        raise SnapshotRefused("invalid_numeric_storage")
    for name, (kind, cap) in fields.items():
        if row[f"{name}_kind"] == "null" and name in (nullable or set()):
            result[name] = None
            continue
        data = row[name]
        if row[f"{name}_kind"] != kind or not isinstance(data, bytes) or len(data) > cap:
            raise SnapshotRefused("invalid_or_oversized_storage")
        result[name] = data.decode("utf-8") if kind == "text" else data
    return result


def _sql(value: str) -> str:
    return " ".join(re.sub(r"\bIF NOT EXISTS\b", "", value).split())


@contextmanager
def _database(
    path: Path, version: int, statements: Sequence[str], automatic: Mapping[str, int]
) -> Iterator[sqlite3.Connection]:
    conn = None
    try:
        check_private_file(path)
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 0")
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("BEGIN")
        if conn.execute("PRAGMA user_version").fetchone()[0] != version:
            raise SnapshotRefused("unexpected_schema_version")
        expected: dict[str, tuple[str, str | None]] = {}
        for statement in statements:
            match = re.match(r"CREATE (?:UNIQUE )?(TABLE|INDEX) IF NOT EXISTS (\w+)", statement)
            if match is not None:
                expected[match[2]] = (match[1].lower(), _sql(statement))
        for table, count in automatic.items():
            for index in range(1, count + 1):
                expected[f"sqlite_autoindex_{table}_{index}"] = ("index", None)
        if version == 3:
            expected["sqlite_sequence"] = ("table", "CREATE TABLE sqlite_sequence(name,seq)")
        rows = conn.execute(
            f"SELECT substr(name,1,129), substr(type,1,17), substr(CAST(sql AS BLOB),1,{MAX_SCHEMA_SQL_BYTES + 1}) FROM sqlite_schema LIMIT {MAX_SCHEMA_ROWS + 1}"
        ).fetchall()
        if len(rows) > MAX_SCHEMA_ROWS:
            raise SnapshotRefused("schema_too_large")
        actual = {}
        for name, kind, sql in rows:
            if (
                not isinstance(name, str)
                or len(name) > 128
                or name in actual
                or (sql is not None and len(sql) > MAX_SCHEMA_SQL_BYTES)
            ):
                raise SnapshotRefused("invalid_schema")
            actual[name] = (kind, None if sql is None else _sql(sql.decode("utf-8")))
        if actual != expected:
            raise SnapshotRefused("unexpected_schema")
        yield conn
    finally:
        if conn is not None:
            conn.close()


def _counter(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNTER


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise SnapshotRefused("invalid_uuid")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotRefused("duplicate_json_key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise SnapshotRefused("nonfinite_json")


def _json(value: str) -> dict[str, Any]:
    parsed = json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(parsed, dict):
        raise SnapshotRefused("invalid_json_object")
    return parsed
