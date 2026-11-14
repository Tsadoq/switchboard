import sqlite3
from dataclasses import astuple
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from agent_board.night.fixture_gate_dto import (
    ORIGIN,
    ZERO_UUID,
    Context,
    FixtureBootstrap,
    NormalAttestation,
    ResultRecord,
    RuntimeOwner,
    TerminalResult,
    TrustedAssertions,
    encode_record,
)
from agent_board.night.fixture_gate_sql import (
    COMPLETION_LAUNCH_QUERY,
    COMPLETION_OPERATION_QUERY,
    DDL,
    IDENTITY_QUERY,
    StoreRefusal,
    _current,
    _validate,
)

CONTEXT = Context(1, 1, "10000000-0000-4000-8000-000000000000", ORIGIN, "a" * 64)
OPERATION = "20000000-0000-4000-8000-000000000000"
LAUNCH = "30000000-0000-4000-8000-000000000000"
RUN_ID = "40000000-0000-4000-8000-000000000000"
OTHER = "50000000-0000-4000-8000-000000000000"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@pytest.fixture
def database(tmp_path: Path) -> Any:
    conn = connect(tmp_path / "store.sqlite")
    for statement in DDL:
        conn.execute(statement)
    conn.execute("PRAGMA user_version=1")
    reference = CONTEXT.reference(ZERO_UUID, ZERO_UUID)
    conn.execute(
        "INSERT INTO identities VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ZERO_UUID, ZERO_UUID, *astuple(CONTEXT), None, None, "BOOTSTRAP"),
    )
    conn.execute(
        "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            reference,
            ZERO_UUID,
            ZERO_UUID,
            *astuple(CONTEXT),
            "FIXTURE_BOOTSTRAP_V1",
            encode_record(FixtureBootstrap(CONTEXT)),
        ),
    )
    conn.execute(
        "INSERT INTO control VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (1, *astuple(CONTEXT), "IDLE", ZERO_UUID, ZERO_UUID, reference, None),
    )
    try:
        yield conn
    finally:
        conn.close()


def pending(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO identities VALUES(?,?,?,?,?,?,?,?,?,?)",
        (OPERATION, LAUNCH, *astuple(CONTEXT), RUN_ID, "b" * 64, "PENDING"),
    )
    conn.execute(
        "UPDATE control SET state='PENDING',operation=?,launch=?,completion_ref=NULL",
        (OPERATION, LAUNCH),
    )


def test_bootstrap_four_validation_and_four_bundle_statements(database: sqlite3.Connection) -> None:
    database.execute("BEGIN")
    trace: list[str] = []
    database.set_trace_callback(trace.append)
    _validate(database)
    observation = _current(database, CONTEXT).observation
    database.set_trace_callback(None)
    assert len(trace) == 8
    assert observation.classification == "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP"
    assert observation.context.origin == ORIGIN
    assert isinstance(observation.completion, FixtureBootstrap)
    assert len([sql for sql in trace if "FROM completions" in sql]) == 2


@pytest.mark.parametrize("fault_locked", [False, True])
def test_matching_normal_completion_and_retained_fault_history(
    database: sqlite3.Connection, fault_locked: bool
) -> None:
    pending(database)
    owner = RuntimeOwner(OPERATION, LAUNCH, RUN_ID, "b" * 64)
    attestation = NormalAttestation(
        CONTEXT,
        owner,
        "LATCH_SUCCESS",
        TerminalResult(
            "COMMITTED",
            "NOT_ATTEMPTED",
            "CLOSED",
            False,
            False,
            False,
            None,
            None,
            ResultRecord(RUN_ID, "b" * 64, "quarantined", True, True),
        ),
        TrustedAssertions(True, True, 123, 0),
    )
    reference = CONTEXT.reference(OPERATION, LAUNCH)
    database.execute(
        "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            reference,
            OPERATION,
            LAUNCH,
            *astuple(CONTEXT),
            "NORMAL_ATTESTATION_V1",
            encode_record(attestation),
        ),
    )
    database.execute("UPDATE identities SET status='COMPLETED' WHERE operation=?", (OPERATION,))
    database.execute("UPDATE control SET state='IDLE',completion_ref=?", (reference,))
    if fault_locked:
        database.execute(
            "UPDATE control SET state='FAULT_LOCKED',completion_ref=NULL,fault_reason='RESOURCE_UNCERTAIN'"
        )
    database.execute("BEGIN")
    _validate(database)
    observation = _current(database, CONTEXT).observation
    expected = "FAULT_LOCKED" if fault_locked else "IDLE_WITH_STRUCTURALLY_VALID_ATTESTATION"
    assert observation.classification == expected
    assert observation.completion == attestation
    assert attestation.result.record is not None and attestation.result.record.uncertain


def test_pending_requires_joint_completion_absence(database: sqlite3.Connection) -> None:
    pending(database)
    database.execute("BEGIN")
    _validate(database)
    current = _current(database, CONTEXT)
    assert current.observation.classification == "PENDING"
    assert current.observation.completion is None


@pytest.mark.parametrize("status", ["PENDING", "FAULTED"])
def test_orphan_other_operation_current_launch_refuses_with_valid_indexes(
    database: sqlite3.Connection, status: str
) -> None:
    pending(database)
    if status == "FAULTED":
        database.execute("UPDATE identities SET status='FAULTED' WHERE operation=?", (OPERATION,))
        database.execute("UPDATE control SET state='FAULT_LOCKED',fault_reason='RESOURCE_UNCERTAIN'")
    database.execute("PRAGMA foreign_keys=OFF")
    database.execute(
        "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            CONTEXT.reference(OTHER, LAUNCH),
            OTHER,
            LAUNCH,
            *astuple(CONTEXT),
            "NORMAL_ATTESTATION_V1",
            b"{}",
        ),
    )
    database.execute("PRAGMA foreign_keys=ON")
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(database.execute("PRAGMA foreign_key_check").fetchall()) == 1
    assert database.execute(COMPLETION_OPERATION_QUERY, (OPERATION,)).fetchall() == []
    assert len(database.execute(COMPLETION_LAUNCH_QUERY, (LAUNCH,)).fetchall()) == 1
    before = database.total_changes
    database.execute("BEGIN")
    trace: list[str] = []
    database.set_trace_callback(trace.append)
    _validate(database)
    with pytest.raises(StoreRefusal, match="ATTESTATION_REFUSED"):
        _current(database, CONTEXT)
    database.set_trace_callback(None)
    assert len(trace) == 8 and database.total_changes == before
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in trace)


@pytest.mark.parametrize("fault_locked", [False, True])
def test_bootstrap_history_launch_mismatch_refuses(database: sqlite3.Connection, fault_locked: bool) -> None:
    if fault_locked:
        database.execute(
            "UPDATE control SET state='FAULT_LOCKED',completion_ref=NULL,fault_reason='OPERATOR_HOLD'"
        )
    database.execute("UPDATE completions SET launch=?", (LAUNCH,))
    database.execute("BEGIN")
    with pytest.raises(StoreRefusal, match="ATTESTATION_REFUSED"):
        _current(database, CONTEXT)


def test_preserved_bootstrap_cannot_override_fault_state(database: sqlite3.Connection) -> None:
    database.execute(
        "UPDATE control SET state='FAULT_LOCKED',completion_ref=NULL,fault_reason='OPERATOR_HOLD'"
    )
    database.execute("BEGIN")
    current = _current(database, CONTEXT)
    assert current.observation.classification == "FAULT_LOCKED"
    assert isinstance(current.observation.completion, FixtureBootstrap)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE extra(value)",
        "CREATE INDEX extra ON identities(status)",
        "PRAGMA user_version=2",
        "PRAGMA foreign_keys=OFF",
    ],
)
def test_schema_header_and_extra_objects_refuse(database: sqlite3.Connection, statement: str) -> None:
    database.execute(statement)
    database.execute("BEGIN")
    with pytest.raises(StoreRefusal):
        _validate(database)


def test_wal_mode_refuses(database: sqlite3.Connection) -> None:
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("BEGIN")
    with pytest.raises(StoreRefusal):
        _validate(database)


def test_snapshot_required(database: sqlite3.Connection) -> None:
    for operation in (lambda: _validate(database), lambda: _current(database, CONTEXT)):
        with pytest.raises(StoreRefusal, match="SNAPSHOT_FAILURE"):
            operation()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE control SET completion_ref=NULL",
        "UPDATE identities SET run_id='invalid'",
        "UPDATE completions SET body=zeroblob(4097)",
        "UPDATE completions SET body='not a blob'",
        "UPDATE completions SET kind='NORMAL_ATTESTATION_V1'",
        "UPDATE completions SET ref='wrong'",
        "UPDATE control SET generation='50000000-0000-4000-8000-000000000000'",
        "UPDATE control SET origin=zeroblob(1000)",
        "UPDATE control SET fault_reason='OPERATOR_HOLD'",
    ],
)
def test_invalid_current_rows_refuse(database: sqlite3.Connection, statement: str) -> None:
    database.execute("PRAGMA foreign_keys=OFF")
    database.execute(statement)
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("BEGIN")
    with pytest.raises(StoreRefusal):
        _current(database, CONTEXT)


def test_extra_control_row_refuses(database: sqlite3.Connection) -> None:
    database.execute("PRAGMA ignore_check_constraints=ON")
    database.execute(
        "INSERT INTO control SELECT 2,store_version,policy_version,generation,origin,supervisor_release_sha256,state,operation,launch,completion_ref,fault_reason FROM control WHERE id=1"
    )
    database.execute("PRAGMA ignore_check_constraints=OFF")
    database.execute("BEGIN")
    with pytest.raises(StoreRefusal):
        _current(database, CONTEXT)


def test_reader_retains_coherent_snapshot_and_does_not_observe_half_publication(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    writer = connect(tmp_path / "store.sqlite")
    try:
        database.execute("BEGIN")
        _validate(database)
        before = _current(database, CONTEXT)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE control SET state='FAULT_LOCKED',completion_ref=NULL,fault_reason='OPERATOR_HOLD'"
        )
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            writer.execute("COMMIT")
        assert _current(database, CONTEXT) == before
        database.execute("COMMIT")
        writer.execute("COMMIT")
        database.execute("BEGIN")
        assert _current(database, CONTEXT).observation.classification == "FAULT_LOCKED"
    finally:
        writer.close()


def test_completion_lookups_have_fixed_indexed_search_plans(database: sqlite3.Connection) -> None:
    for query, value, index in (
        (IDENTITY_QUERY, ZERO_UUID, "sqlite_autoindex_identities_1"),
        (COMPLETION_OPERATION_QUERY, ZERO_UUID, "sqlite_autoindex_completions_2"),
        (COMPLETION_LAUNCH_QUERY, ZERO_UUID, "sqlite_autoindex_completions_3"),
    ):
        plans = database.execute("EXPLAIN QUERY PLAN " + query, (value,)).fetchall()
        assert len(plans) == 1
        assert "SEARCH" in plans[0][3] and index in plans[0][3]


@pytest.mark.parametrize("history", [10, 10000, 100000])
def test_current_bundle_sql_bound_and_search_plans_with_history(
    database: sqlite3.Connection, history: int
) -> None:
    template = encode_record(
        NormalAttestation(
            CONTEXT,
            RuntimeOwner(OPERATION, LAUNCH, RUN_ID, "b" * 64),
            "LATCH_SUCCESS",
            TerminalResult(
                "COMMITTED",
                "NOT_ATTEMPTED",
                "CLOSED",
                False,
                False,
                False,
                None,
                None,
                ResultRecord(RUN_ID, "b" * 64, "stopping", True, False),
            ),
            TrustedAssertions(True, True, 123, 0),
        )
    )
    identities: list[tuple[Any, ...]] = []
    completions: list[tuple[Any, ...]] = []
    for number in range(history):
        operation = str(UUID(int=number + 100000))
        launch = str(UUID(int=number + 1000000))
        body = template.replace(OPERATION.encode(), operation.encode()).replace(
            LAUNCH.encode(), launch.encode()
        )
        identities.append((operation, launch, *astuple(CONTEXT), RUN_ID, "b" * 64, "COMPLETED"))
        completions.append(
            (
                CONTEXT.reference(operation, launch),
                operation,
                launch,
                *astuple(CONTEXT),
                "NORMAL_ATTESTATION_V1",
                body,
            )
        )
    database.execute("BEGIN IMMEDIATE")
    database.executemany("INSERT INTO identities VALUES(?,?,?,?,?,?,?,?,?,?)", identities)
    database.executemany("INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)", completions)
    database.execute("COMMIT")
    test_completion_lookups_have_fixed_indexed_search_plans(database)
    database.execute("BEGIN")
    trace: list[str] = []
    database.set_trace_callback(trace.append)
    _validate(database)
    observed = _current(database, CONTEXT)
    database.set_trace_callback(None)
    assert observed.observation.classification == "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP"
    assert len(trace) == 8
