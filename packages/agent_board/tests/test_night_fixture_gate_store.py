import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from agent_board.night import fixture_gate_store as store
from agent_board.night.fixture_gate_dto import (
    ORIGIN,
    BootstrapSelector,
    Context,
    NormalAttestation,
    ResultRecord,
    RuntimeOwner,
    RuntimeSelector,
    TerminalResult,
    TrustedAssertions,
    encode_record,
)
from agent_board.night.fixture_gate_sql import FAULT_REASONS
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

CONTEXT = Context(1, 1, "10000000-0000-4000-8000-000000000000", ORIGIN, "a" * 64)
OWNER = RuntimeOwner(
    "20000000-0000-4000-8000-000000000000",
    "30000000-0000-4000-8000-000000000000",
    "40000000-0000-4000-8000-000000000000",
    "b" * 64,
)
CONNECT = sqlite3.connect


def attestation(
    *, owner: RuntimeOwner = OWNER, terminal: str = "LATCH_SUCCESS", changed: bool = False
) -> NormalAttestation:
    result = (
        TerminalResult(
            "COMMITTED",
            "NOT_ATTEMPTED",
            "CLOSED",
            changed,
            changed,
            False,
            None,
            None,
            ResultRecord(owner.run_id, owner.fence_sha256, "quarantined", True, True),
        )
        if terminal == "LATCH_SUCCESS"
        else TerminalResult(
            "ROLLED_BACK", "COMMITTED", "CLOSED", False, None, changed, "stale_fence", None, None
        )
    )
    return NormalAttestation(CONTEXT, owner, terminal, result, TrustedAssertions(True, True, 123, 0))


def committed(result: store.WriteResult) -> None:
    assert result.mutation == MutationOutcome.COMMITTED
    assert result.cleanup == CleanupOutcome.CLOSED
    assert result.changed is True
    assert result.fault is None and result.refusal is None


def classification(path: Path) -> str:
    result = store.observe(path, context=CONTEXT)
    assert result.cleanup == CleanupOutcome.CLOSED
    assert result.fault is None and result.refusal is None
    assert result.observation is not None
    return result.observation.classification


@pytest.fixture
def gate(tmp_path: Path) -> Path:
    path = tmp_path / "gate.sqlite"
    result = store.provision_fixture(path, context=CONTEXT)
    committed(result)
    assert result.observed_created_path and result.path_publication == store.Publication.CONFIRMED
    return path


@pytest.mark.parametrize("terminal", ["LATCH_SUCCESS", "DOMAIN_REFUSED"])
@pytest.mark.parametrize("changed", [False, True])
def test_fresh_file_begin_complete_and_fresh_classification(gate: Path, terminal: str, changed: bool) -> None:
    assert stat.S_IMODE(gate.stat().st_mode) == 0o600
    assert classification(gate) == "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP"
    committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    assert classification(gate) == "PENDING"
    record = attestation(terminal=terminal, changed=changed)
    committed(store.complete_operation(gate, context=CONTEXT, attestation=record))
    observed = store.observe(gate, context=CONTEXT).observation
    assert observed is not None
    assert observed.classification == "IDLE_WITH_STRUCTURALLY_VALID_ATTESTATION"
    assert observed.completion == record
    assert observed.selector == RuntimeSelector(OWNER)


@pytest.mark.parametrize("initial", ["bootstrap", "pending", "completed"])
@pytest.mark.parametrize("reason", sorted(FAULT_REASONS))
def test_lock_preserves_owner_and_validated_history(gate: Path, initial: str, reason: str) -> None:
    if initial != "bootstrap":
        committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    if initial == "completed":
        committed(store.complete_operation(gate, context=CONTEXT, attestation=attestation()))
    before = store.observe(gate, context=CONTEXT).observation
    assert before is not None
    committed(
        store.lock_fault(
            gate,
            context=CONTEXT,
            selector=before.selector,
            expected_state="PENDING" if initial == "pending" else "IDLE",
            reason=reason,
        )
    )
    after = store.observe(gate, context=CONTEXT).observation
    assert after is not None and after.classification == "FAULT_LOCKED"
    assert after.selector == before.selector and after.completion == before.completion
    assert after.fault_reason == reason
    result = store.lock_fault(
        gate,
        context=CONTEXT,
        selector=before.selector,
        expected_state="IDLE",
        reason=reason,
    )
    assert result.refusal == store.Refusal.STATE_MISMATCH
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
    assert classification(gate) == "FAULT_LOCKED"
    assert store.begin_operation(gate, context=CONTEXT, owner=OWNER).refusal == store.Refusal.STATE_MISMATCH


@pytest.mark.parametrize("kind", ["empty", "corrupt", "valid", "symlink", "directory", "journal"])
def test_provision_never_adopts_truncates_or_replaces_an_existing_path(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "gate.sqlite"
    if kind == "valid":
        committed(store.provision_fixture(path, context=CONTEXT))
    elif kind == "symlink":
        path.symlink_to(tmp_path / "absent")
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"corrupt" if kind == "corrupt" else b"")
        if kind == "journal":
            path.with_name(path.name + "-journal").write_bytes(b"retained")
    metadata = path.lstat()
    data = path.read_bytes() if path.is_file() else None
    result = store.provision_fixture(path, context=CONTEXT)
    assert result.refusal == store.Refusal.PREEXISTING_PATH
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED
    assert result.changed is False and not result.observed_created_path
    assert result.path_publication == store.Publication.NOT_ATTEMPTED
    assert path.lstat() == metadata
    if data is not None:
        assert path.read_bytes() == data


@pytest.mark.parametrize("operation", ["observe", "begin", "complete", "fault"])
def test_runtime_missing_file_never_creates_it(tmp_path: Path, operation: str) -> None:
    path = tmp_path / "absent.sqlite"
    result = invoke(operation, path)
    assert result.fault == store.Fault.FILE_FAILURE
    assert not path.exists() and list(tmp_path.iterdir()) == []


def invoke(action: str, path: Path) -> store.WriteResult | store.ReadResult:
    if action == "observe":
        return store.observe(path, context=CONTEXT)
    if action == "begin":
        return store.begin_operation(path, context=CONTEXT, owner=OWNER)
    if action == "complete":
        return store.complete_operation(path, context=CONTEXT, attestation=attestation())
    if action == "provision":
        return store.provision_fixture(path, context=CONTEXT)
    return store.lock_fault(
        path,
        context=CONTEXT,
        selector=RuntimeSelector(OWNER),
        expected_state="PENDING",
        reason="RESOURCE_UNCERTAIN",
    )


@pytest.mark.parametrize("change", ["operation", "launch", "run_id", "fence_sha256"])
def test_complete_requires_the_entire_current_owner(gate: Path, change: str) -> None:
    committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    value = "c" * 64 if change == "fence_sha256" else "50000000-0000-4000-8000-000000000000"
    wrong = replace(OWNER, **{change: value})
    before = gate.read_bytes()
    result = store.complete_operation(gate, context=CONTEXT, attestation=attestation(owner=wrong))
    assert result.refusal == store.Refusal.OWNER_MISMATCH and result.changed is False
    assert result.mutation == MutationOutcome.ROLLED_BACK
    assert gate.read_bytes() == before and classification(gate) == "PENDING"


@pytest.mark.parametrize("field", ["operation", "launch"])
def test_retained_identity_refuses_reuse_after_completion(gate: Path, field: str) -> None:
    committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    committed(store.complete_operation(gate, context=CONTEXT, attestation=attestation()))
    new_owner = replace(OWNER, operation=str(UUID(int=17)), launch=str(UUID(int=18)))
    recycled = replace(new_owner, **{field: getattr(OWNER, field)})
    result = store.begin_operation(gate, context=CONTEXT, owner=recycled)
    assert result.refusal == store.Refusal.REPLAY_REFUSED and result.mutation == MutationOutcome.ROLLED_BACK
    committed(store.begin_operation(gate, context=CONTEXT, owner=new_owner))
    assert classification(gate) == "PENDING"


class Probe:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        faults: dict[str, str] | None = None,
        skip: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.connections: list[sqlite3.Connection] = []
        self.closes = 0
        self.faults = dict(faults or {})
        self.skip = dict(skip or {})
        probe = self

        class Connection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                probe.calls.append(sql)
                matched = next((key for key in probe.faults if sql.startswith(key)), None)
                fault = None
                if matched is not None:
                    if probe.skip.get(matched, 0):
                        probe.skip[matched] -= 1
                    else:
                        fault = probe.faults.pop(matched)
                if fault == "before":
                    raise sqlite3.OperationalError("injected before execute")
                result = super().execute(sql, parameters)
                if fault == "after":
                    raise sqlite3.OperationalError("injected after execute")
                return result

            def close(self) -> None:
                probe.closes += 1
                fault = probe.faults.pop("close", None)
                if fault == "before":
                    raise sqlite3.OperationalError("injected before close")
                super().close()
                if fault == "after":
                    raise sqlite3.OperationalError("injected after close")

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            assert kwargs["uri"] is True and kwargs["timeout"] == 0 and kwargs["isolation_level"] is None
            assert "mode=ro" in args[0] or "mode=rw" in args[0]
            connection = CONNECT(*args, **kwargs, factory=Connection)
            self.connections.append(connection)
            return connection

        monkeypatch.setattr(sqlite3, "connect", connect)

    def dispose(self) -> None:
        for connection in self.connections:
            sqlite3.Connection.close(connection)


@pytest.fixture
def probe_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Probe]]:
    probes: list[Probe] = []

    def create(**kwargs: Any) -> Probe:
        probe = Probe(monkeypatch, **kwargs)
        probes.append(probe)
        return probe

    yield create
    for probe in probes:
        probe.dispose()


@pytest.mark.parametrize(
    ("action", "maximum"),
    [("observe", 14), ("begin", 20), ("complete", 17), ("fault", 16), ("provision", 22)],
)
def test_complete_api_success_sql_budgets_and_fresh_connections(
    gate: Path,
    action: str,
    maximum: int,
    probe_factory: Callable[..., Probe],
) -> None:
    if action in ("complete", "fault"):
        committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    path = gate.with_name("new.sqlite") if action == "provision" else gate
    probe = probe_factory()
    result = invoke(action, path)
    assert result.fault is None and result.refusal is None
    assert len(probe.calls) == maximum
    assert len(probe.connections) == 1 and probe.closes == 1
    assert probe.calls[-1] == "COMMIT"
    second = store.observe(path, context=CONTEXT)
    assert second.observation is not None
    assert len(probe.connections) == 2 and probe.closes == 2


@pytest.mark.parametrize("action", ["observe", "begin", "complete", "fault", "provision"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_commit_exceptions_never_become_known_mutations_or_observations(
    gate: Path,
    action: str,
    when: str,
    probe_factory: Callable[..., Probe],
) -> None:
    if action in ("complete", "fault"):
        committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    path = gate.with_name("new.sqlite") if action == "provision" else gate
    probe = probe_factory(faults={"COMMIT": when})
    result = invoke(action, path)
    assert result.fault == store.Fault.SQLITE_FAILURE and result.cleanup == CleanupOutcome.CLOSED
    if isinstance(result, store.ReadResult):
        assert result.observation is None
    else:
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
    assert probe.calls.count("ROLLBACK") == (1 if when == "before" else 0)
    maximum = {"observe": 14, "begin": 20, "complete": 17, "fault": 16, "provision": 22}[action]
    assert len(probe.calls) == maximum + (when == "before")
    if isinstance(result, store.ProvisionResult):
        assert result.observed_created_path and result.path_publication == store.Publication.UNKNOWN
        assert store.provision_fixture(path, context=CONTEXT).refusal == store.Refusal.PREEXISTING_PATH
    later = store.observe(path, context=CONTEXT)
    if when == "after":
        assert later.observation is not None
        if action == "complete":
            assert later.observation.classification == "IDLE_WITH_STRUCTURALLY_VALID_ATTESTATION"
    elif action != "provision":
        assert later.observation is not None
        assert later.observation.classification == (
            "PENDING" if action in ("complete", "fault") else "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP"
        )
    if isinstance(result, store.WriteResult):
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None


@pytest.mark.parametrize("action", ["observe", "begin", "complete", "fault", "provision"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_known_commit_survives_close_failure_but_never_exposes_a_read(
    gate: Path,
    action: str,
    when: str,
    probe_factory: Callable[..., Probe],
) -> None:
    if action in ("complete", "fault"):
        committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    probe = probe_factory(faults={"close": when})
    path = gate.with_name("new.sqlite") if action == "provision" else gate
    result = invoke(action, path)
    assert result.cleanup == CleanupOutcome.UNCERTAIN and result.fault == store.Fault.CLOSE_FAILURE
    if isinstance(result, store.ReadResult):
        assert result.observation is None
    else:
        assert result.mutation == MutationOutcome.COMMITTED and result.changed is True
    assert probe.closes == 1 and "ROLLBACK" not in probe.calls
    if isinstance(result, store.ProvisionResult):
        assert result.path_publication == store.Publication.UNKNOWN


@pytest.mark.parametrize("rollback_when", ["before", "after"])
@pytest.mark.parametrize("close_when", [None, "before", "after"])
def test_commit_rollback_and_close_faults_are_independent_and_never_retried(
    gate: Path,
    rollback_when: str,
    close_when: str | None,
    probe_factory: Callable[..., Probe],
) -> None:
    faults = {"COMMIT": "before", "ROLLBACK": rollback_when}
    if close_when is not None:
        faults["close"] = close_when
    probe = probe_factory(faults=faults)
    result = store.begin_operation(gate, context=CONTEXT, owner=OWNER)
    assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
    assert result.cleanup == (CleanupOutcome.UNCERTAIN if close_when else CleanupOutcome.CLOSED)
    assert result.fault == (store.Fault.CLOSE_FAILURE if close_when else store.Fault.ROLLBACK_FAILURE)
    assert probe.calls.count("ROLLBACK") == 1 and probe.calls.count("COMMIT") == 1 and probe.closes == 1
    if close_when != "before":
        assert classification(gate) == "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP"


@pytest.mark.parametrize("sync_call", [1, 2])
def test_failed_directory_sync_does_not_rewrite_sqlite_knowledge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_call: int,
) -> None:
    original = os.fsync
    calls = 0

    def fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == sync_call:
            raise OSError("injected directory fsync")
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    path = tmp_path / "gate.sqlite"
    result = store.provision_fixture(path, context=CONTEXT)
    assert result.observed_created_path and result.path_publication == store.Publication.UNKNOWN
    assert result.fault == store.Fault.DIRECTORY_PUBLICATION_FAILURE
    assert result.cleanup == CleanupOutcome.CLOSED
    assert result.mutation == (MutationOutcome.NOT_ATTEMPTED if sync_call == 1 else MutationOutcome.COMMITTED)
    assert result.changed is (sync_call == 2)
    if sync_call == 2:
        assert classification(path) == "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP"
    else:
        assert path.read_bytes() == b""
    assert store.provision_fixture(path, context=CONTEXT).refusal == store.Refusal.PREEXISTING_PATH


@pytest.mark.parametrize("action", ["observe", "begin", "complete", "fault"])
def test_current_launch_orphan_refuses_before_any_dml(
    gate: Path,
    action: str,
    probe_factory: Callable[..., Probe],
) -> None:
    committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    orphan_owner = replace(OWNER, operation=str(UUID(int=999)))
    with closing(CONNECT(gate, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                CONTEXT.reference(orphan_owner.operation, orphan_owner.launch),
                orphan_owner.operation,
                orphan_owner.launch,
                *astuple(CONTEXT),
                "NORMAL_ATTESTATION_V1",
                encode_record(attestation(owner=orphan_owner)),
            ),
        )
    before = gate.read_bytes()
    probe = probe_factory()
    result = invoke(action, gate)
    assert result.refusal == store.Refusal.ATTESTATION_REFUSED
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in probe.calls)
    assert gate.read_bytes() == before
    assert probe.calls[-1] == "ROLLBACK" and probe.closes == 1


@pytest.mark.parametrize("action", ["observe", "begin", "complete", "fault"])
@pytest.mark.parametrize(
    "kind", ["empty", "corrupt", "symlink", "hardlink", "permissions", "parent", "wal", "journal"]
)
def test_real_filesystem_refusals_do_not_repair_or_create_sidecars(
    gate: Path,
    action: str,
    kind: str,
    probe_factory: Callable[..., Probe],
) -> None:
    if kind == "empty":
        gate.write_bytes(b"")
    elif kind == "corrupt":
        gate.write_bytes(b"corrupt")
    elif kind == "symlink":
        actual = gate.with_name("actual.sqlite")
        gate.rename(actual)
        gate.symlink_to(actual)
    elif kind == "hardlink":
        os.link(gate, gate.with_name("link.sqlite"))
    elif kind == "permissions":
        gate.chmod(0o644)
    elif kind == "parent":
        gate.parent.chmod(0o755)
    elif kind == "wal":
        with closing(CONNECT(gate, isolation_level=None)) as conn:
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    else:
        gate.with_name(gate.name + "-journal").write_bytes(b"retained uncertain journal")
    before = {path.name: path.read_bytes() for path in gate.parent.iterdir()}
    probe = probe_factory()
    result = invoke(action, gate)
    assert result.fault == store.Fault.FILE_FAILURE
    assert not probe.calls and not probe.connections
    assert {path.name: path.read_bytes() for path in gate.parent.iterdir()} == before
    if isinstance(result, store.ReadResult):
        assert result.observation is None
    else:
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False


@pytest.mark.parametrize("action", ["observe", "begin", "complete", "fault"])
def test_wrong_context_refuses_inside_snapshot_without_mutation(
    gate: Path,
    action: str,
    probe_factory: Callable[..., Probe],
) -> None:
    with closing(CONNECT(gate, isolation_level=None)) as conn:
        conn.execute("UPDATE control SET generation=?", (str(UUID(int=19)),))
    before = gate.read_bytes()
    probe = probe_factory()
    result = invoke(action, gate)
    assert result.refusal == store.Refusal.CONTEXT_MISMATCH
    assert gate.read_bytes() == before
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in probe.calls)


@pytest.mark.parametrize("selector", [BootstrapSelector(), RuntimeSelector(OWNER)])
@pytest.mark.parametrize("initial", ["bootstrap", "pending", "completed"])
def test_bootstrap_and_runtime_selectors_are_not_interchangeable(
    gate: Path,
    selector: BootstrapSelector | RuntimeSelector,
    initial: str,
) -> None:
    if initial != "bootstrap":
        committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    if initial == "completed":
        committed(store.complete_operation(gate, context=CONTEXT, attestation=attestation()))
    before = gate.read_bytes()
    result = store.lock_fault(
        gate,
        context=CONTEXT,
        selector=selector,
        expected_state="PENDING" if initial == "pending" else "IDLE",
        reason="RESOURCE_UNCERTAIN",
    )
    if (initial == "bootstrap") == isinstance(selector, BootstrapSelector):
        committed(result)
    else:
        assert result.refusal == store.Refusal.OWNER_MISMATCH
        assert result.mutation == MutationOutcome.ROLLED_BACK and gate.read_bytes() == before


@pytest.mark.parametrize("field", ["operation", "launch"])
def test_requested_key_orphan_refuses_before_begin_dml(
    gate: Path,
    field: str,
    probe_factory: Callable[..., Probe],
) -> None:
    orphan = replace(OWNER, **{field: str(UUID(int=27))})
    with closing(CONNECT(gate, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                CONTEXT.reference(orphan.operation, orphan.launch),
                orphan.operation,
                orphan.launch,
                *astuple(CONTEXT),
                "NORMAL_ATTESTATION_V1",
                encode_record(attestation(owner=orphan)),
            ),
        )
    probe = probe_factory()
    result = store.begin_operation(gate, context=CONTEXT, owner=OWNER)
    assert result.refusal == store.Refusal.REPLAY_REFUSED
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in probe.calls)


@pytest.mark.parametrize("statement", ["INSERT INTO completions", "UPDATE identities", "UPDATE control"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_completion_body_faults_roll_back_record_status_and_reference_together(
    gate: Path,
    statement: str,
    when: str,
    probe_factory: Callable[..., Probe],
) -> None:
    committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    before = gate.read_bytes()
    probe = probe_factory(faults={statement: when})
    result = store.complete_operation(gate, context=CONTEXT, attestation=attestation())
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
    assert result.cleanup == CleanupOutcome.CLOSED and result.fault == store.Fault.SQLITE_FAILURE
    assert probe.calls.count("ROLLBACK") == 1 and "COMMIT" not in probe.calls
    assert gate.read_bytes() == before and classification(gate) == "PENDING"


def test_fresh_durable_observation_does_not_dispose_an_old_live_handle(
    gate: Path,
    probe_factory: Callable[..., Probe],
) -> None:
    probe = probe_factory(faults={"close": "before"})
    result = store.begin_operation(gate, context=CONTEXT, owner=OWNER)
    assert result.mutation == MutationOutcome.COMMITTED and result.cleanup == CleanupOutcome.UNCERTAIN
    retained = probe.connections[0]
    assert classification(gate) == "PENDING"
    assert retained.execute("SELECT state FROM control").fetchone()[0] == "PENDING"
    assert result.cleanup == CleanupOutcome.UNCERTAIN and result.fault == store.Fault.CLOSE_FAILURE
