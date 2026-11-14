import sqlite3
from collections.abc import Callable
from contextlib import closing
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from agent_board.night import fixture_gate_store as store
from agent_board.night.fixture_gate_dto import ZERO_UUID, BootstrapSelector, RuntimeSelector
from agent_board.night.fixture_gate_sql import (
    CATALOG_QUERY,
    COMPLETION_LAUNCH_QUERY,
    COMPLETION_OPERATION_QUERY,
    CONTROL_QUERY,
    IDENTITY_QUERY,
)
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_fixture_gate_store import (
        CONNECT,
        CONTEXT,
        OWNER,
        Probe,
        attestation,
        committed,
        gate,
        invoke,
        probe_factory,
    )
else:
    from test_night_fixture_gate_store import (
        CONNECT,
        CONTEXT,
        OWNER,
        Probe,
        attestation,
        committed,
        gate,
        invoke,
        probe_factory,
    )

__all__ = ["gate", "probe_factory"]


def stages(action: str) -> list[tuple[str, str, int]]:
    statements = [
        ("setup", "PRAGMA busy_timeout=0", 0),
        ("setup", "PRAGMA trusted_schema=OFF", 0),
        ("setup", "PRAGMA foreign_keys=ON", 0),
        ("setup", "PRAGMA query_only=ON" if action == "observe" else "PRAGMA synchronous=EXTRA", 0),
    ]
    if action == "provision":
        statements.append(("setup", "PRAGMA journal_mode=DELETE", 0))
    statements.append(("begin", "BEGIN" if action == "observe" else "BEGIN IMMEDIATE", 0))
    if action == "provision":
        statements.extend(
            ("body", sql, 0)
            for sql in (
                "CREATE TABLE control",
                "CREATE TABLE identities",
                "CREATE TABLE completions",
                "PRAGMA user_version=1",
                "INSERT INTO identities",
                "INSERT INTO completions",
                "INSERT INTO control",
            )
        )
    statements.extend(
        (
            ("body", "PRAGMA journal_mode", int(action == "provision")),
            ("body", "PRAGMA user_version", int(action == "provision")),
            ("body", "PRAGMA foreign_keys", 1),
            ("body", CATALOG_QUERY, 0),
            ("body", CONTROL_QUERY, 0),
            ("body", IDENTITY_QUERY, 0),
            ("body", COMPLETION_OPERATION_QUERY, 0),
            ("body", COMPLETION_LAUNCH_QUERY, 0),
        )
    )
    if action == "begin":
        statements.extend(
            (
                "body",
                f"SELECT 1 FROM {table} INDEXED BY sqlite_autoindex_{table}_{index} WHERE {column}=? LIMIT 2",
                0,
            )
            for table, column, index in (
                ("identities", "operation", 1),
                ("identities", "launch", 2),
                ("completions", "operation", 2),
                ("completions", "launch", 3),
            )
        )
        statements.extend(("body", sql, 0) for sql in ("INSERT INTO identities", "UPDATE control"))
    elif action == "complete":
        statements.extend(
            ("body", sql, 0) for sql in ("INSERT INTO completions", "UPDATE identities", "UPDATE control")
        )
    elif action == "fault":
        statements.extend(("body", sql, 0) for sql in ("UPDATE identities", "UPDATE control"))
    statements.append(("commit", "COMMIT", 0))
    return statements


STAGES = [
    (action, position, *stage)
    for action in ("observe", "begin", "complete", "fault", "provision")
    for position, stage in enumerate(stages(action))
]


@pytest.mark.parametrize("action,position,phase,sql,skip", STAGES)
@pytest.mark.parametrize("when", ["before", "after"])
def test_every_public_api_explicit_sql_stage_preserves_failure_knowledge_and_closes_once(
    gate: Path,
    action: str,
    position: int,
    phase: str,
    sql: str,
    skip: int,
    when: str,
    probe_factory: Callable[..., Probe],
) -> None:
    if action in ("complete", "fault"):
        committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    path = gate.with_name("new.sqlite") if action == "provision" else gate
    before = gate.read_bytes()
    probe = probe_factory(faults={sql: when}, skip={sql: skip})
    result = invoke(action, path)
    assert result.fault == store.Fault.SQLITE_FAILURE and result.refusal is None
    assert result.cleanup == CleanupOutcome.CLOSED and probe.closes == 1
    assert not probe.faults
    assert probe.calls[position] == sql or probe.calls[position].startswith(sql)
    rolled_back = phase not in ("setup", "begin") and not (phase == "commit" and when == "after")
    if phase == "begin" and when == "after":
        rolled_back = True
    assert probe.calls.count("ROLLBACK") == int(rolled_back)
    assert len(probe.calls) == position + 1 + int(rolled_back)
    if isinstance(result, store.ReadResult):
        assert result.observation is None
    else:
        expected = (
            MutationOutcome.UNKNOWN
            if phase == "commit"
            else MutationOutcome.NOT_ATTEMPTED
            if phase in ("setup", "begin")
            else MutationOutcome.ROLLED_BACK
        )
        assert result.mutation == expected
        assert result.changed is (None if expected == MutationOutcome.UNKNOWN else False)
    if action != "provision" and not (phase == "commit" and when == "after"):
        assert gate.read_bytes() == before
    if isinstance(result, store.ProvisionResult):
        assert result.observed_created_path and result.path_publication == store.Publication.UNKNOWN
        assert store.provision_fixture(path, context=CONTEXT).refusal == store.Refusal.PREEXISTING_PATH


@pytest.mark.parametrize("rollback_when", ["before", "after"])
@pytest.mark.parametrize("close_when", [None, "before", "after"])
def test_close_can_establish_never_committed_rollback_but_failed_cleanup_is_not_retried(
    gate: Path,
    rollback_when: str,
    close_when: str | None,
    probe_factory: Callable[..., Probe],
) -> None:
    faults = {"UPDATE control": "after", "ROLLBACK": rollback_when}
    if close_when is not None:
        faults["close"] = close_when
    probe = probe_factory(faults=faults)
    result = store.begin_operation(gate, context=CONTEXT, owner=OWNER)
    assert "COMMIT" not in probe.calls and probe.calls.count("ROLLBACK") == 1 and probe.closes == 1
    expected = MutationOutcome.UNKNOWN if close_when else MutationOutcome.ROLLED_BACK
    assert result.mutation == expected and result.changed is (None if close_when else False)
    assert result.fault == (store.Fault.CLOSE_FAILURE if close_when else store.Fault.ROLLBACK_FAILURE)
    assert result.cleanup == (CleanupOutcome.UNCERTAIN if close_when else CleanupOutcome.CLOSED)


@pytest.mark.parametrize("initial", ["bootstrap", "completed", "faulted_bootstrap", "faulted_completed"])
@pytest.mark.parametrize("field", ["operation", "launch"])
@pytest.mark.parametrize("action", ["observe", "begin", "complete", "fault"])
def test_one_sided_completion_lookup_refuses_including_preserved_fault_history(
    gate: Path,
    initial: str,
    field: str,
    action: str,
    probe_factory: Callable[..., Probe],
) -> None:
    if initial.endswith("completed"):
        committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
        committed(store.complete_operation(gate, context=CONTEXT, attestation=attestation()))
    if initial.startswith("faulted"):
        selector = RuntimeSelector(OWNER) if initial.endswith("completed") else BootstrapSelector()
        committed(
            store.lock_fault(
                gate, context=CONTEXT, selector=selector, expected_state="IDLE", reason="OPERATOR_HOLD"
            )
        )
    with closing(CONNECT(gate, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        operation = OWNER.operation if initial.endswith("completed") else ZERO_UUID
        conn.execute(f"UPDATE completions SET {field}=? WHERE operation=?", (str(UUID(int=99)), operation))
    before = gate.read_bytes()
    probe = probe_factory()
    result = invoke(action, gate)
    assert result.refusal == store.Refusal.ATTESTATION_REFUSED and result.fault is None
    assert result.cleanup == CleanupOutcome.CLOSED
    assert not any(statement.startswith(("INSERT", "UPDATE", "DELETE")) for statement in probe.calls)
    assert gate.read_bytes() == before
    if isinstance(result, store.ReadResult):
        assert result.observation is None
    else:
        assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False


@pytest.mark.parametrize("action", ["observe", "begin", "complete", "fault"])
def test_real_path_replacement_after_connect_is_detected_before_setup(
    gate: Path,
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if action in ("complete", "fault"):
        committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    statements: list[str] = []
    handles: list[sqlite3.Connection] = []
    original = gate.read_bytes()

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            statements.append(sql)
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = CONNECT(*args, **kwargs, factory=Connection)
        handles.append(connection)
        gate.rename(gate.with_name("retained-old.sqlite"))
        gate.write_bytes(original)
        gate.chmod(0o600)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    result = invoke(action, gate)
    assert result.fault == store.Fault.FILE_FAILURE and result.cleanup == CleanupOutcome.CLOSED
    assert statements == [] and gate.read_bytes() == original
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        handles[0].execute("SELECT 1")


@pytest.mark.parametrize(
    "field,value",
    [
        ("store_version", True),
        ("policy_version", True),
        ("origin", "PRIVILEGED"),
        ("generation", "bad"),
        ("supervisor_release_sha256", "A" * 64),
    ],
)
def test_mutated_frozen_context_is_revalidated_before_file_creation(
    tmp_path: Path,
    field: str,
    value: Any,
    probe_factory: Callable[..., Probe],
) -> None:
    context = copy(CONTEXT)
    object.__setattr__(context, field, value)
    probe = probe_factory()
    path = tmp_path / "new.sqlite"
    result = store.provision_fixture(path, context=context)
    assert result.refusal == store.Refusal.CONTEXT_MISMATCH
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and not result.observed_created_path
    assert not path.exists() and not probe.calls and not probe.connections


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", ZERO_UUID),
        ("operation", "bad"),
        ("launch", ZERO_UUID),
        ("run_id", True),
        ("fence_sha256", "B" * 64),
    ],
)
def test_mutated_frozen_owner_is_revalidated_before_any_storage_access(
    gate: Path,
    field: str,
    value: Any,
    probe_factory: Callable[..., Probe],
) -> None:
    owner = copy(OWNER)
    object.__setattr__(owner, field, value)
    before = gate.read_bytes()
    probe = probe_factory()
    result = store.begin_operation(gate, context=CONTEXT, owner=owner)
    assert result.refusal == store.Refusal.OWNER_MISMATCH
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
    assert not probe.calls and not probe.connections and gate.read_bytes() == before


@pytest.mark.parametrize("action", ["observe", "begin", "complete", "fault"])
@pytest.mark.parametrize("corruption", ["version", "table", "index", "trigger"])
def test_public_schema_refusal_precedes_dml_and_leaves_artifacts_unchanged(
    gate: Path,
    action: str,
    corruption: str,
    probe_factory: Callable[..., Probe],
) -> None:
    statements = {
        "version": "PRAGMA user_version=2",
        "table": "CREATE TABLE unrelated(value TEXT)",
        "index": "CREATE INDEX unrelated_index ON identities(run_id)",
        "trigger": "CREATE TRIGGER unrelated_trigger AFTER UPDATE ON control BEGIN SELECT 1; END",
    }
    with closing(CONNECT(gate, isolation_level=None)) as conn:
        conn.execute(statements[corruption])
    before = gate.read_bytes()
    probe = probe_factory()
    result = invoke(action, gate)
    assert result.refusal == store.Refusal.SCHEMA_REFUSED and result.fault is None
    assert result.cleanup == CleanupOutcome.CLOSED
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in probe.calls)
    assert gate.read_bytes() == before
    if isinstance(result, store.ReadResult):
        assert result.observation is None
    else:
        assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False


@pytest.mark.parametrize(
    "path", [None, "gate.sqlite", Path("relative.sqlite"), Path("/home/dev/../bad.sqlite")]
)
def test_invalid_paths_return_a_file_fault_without_creation(path: Any) -> None:
    result = store.provision_fixture(path, context=CONTEXT)
    assert result.fault == store.Fault.FILE_FAILURE
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and not result.observed_created_path
