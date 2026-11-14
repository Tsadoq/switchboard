import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from agent_board.night import fixture_gate_store as store
from agent_board.night.fixture_gate_dto import RuntimeSelector, encode_record
from agent_board.night.fixture_gate_sql import (
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
        classification,
        committed,
        gate,
        probe_factory,
    )
else:
    from test_night_fixture_gate_store import (
        CONNECT,
        CONTEXT,
        OWNER,
        Probe,
        attestation,
        classification,
        committed,
        gate,
        probe_factory,
    )

__all__ = ["gate", "probe_factory"]


@pytest.mark.parametrize("when", ["before", "after"])
def test_domain_refusal_and_close_fault_coexist_without_losing_confirmed_rollback(
    gate: Path,
    when: str,
    probe_factory: Callable[..., Probe],
) -> None:
    committed(store.begin_operation(gate, context=CONTEXT, owner=OWNER))
    probe = probe_factory(faults={"close": when})
    wrong = replace(OWNER, fence_sha256="c" * 64)
    result = store.complete_operation(gate, context=CONTEXT, attestation=attestation(owner=wrong))
    assert result.refusal == store.Refusal.OWNER_MISMATCH and result.fault == store.Fault.CLOSE_FAILURE
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
    assert result.cleanup == CleanupOutcome.UNCERTAIN and probe.calls.count("ROLLBACK") == 1


def test_public_reader_completes_one_snapshot_while_public_writer_holds_uncommitted_changes(
    gate: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = Event()
    ready = Event()
    release = Event()

    class Reader(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if sql == CONTROL_QUERY:
                start.set()
                assert ready.wait(20), "writer did not reach commit barrier"
            return cursor

    class Writer(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "COMMIT":
                ready.set()
                assert release.wait(20), "reader did not release writer"
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        factory = Reader if "mode=ro" in args[0] else Writer
        return CONNECT(*args, **kwargs, factory=factory)

    def write() -> store.WriteResult:
        assert start.wait(20), "reader did not reach bundle barrier"
        return store.begin_operation(gate, context=CONTEXT, owner=OWNER)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(write)
        try:
            read = store.observe(gate, context=CONTEXT)
        finally:
            release.set()
        committed(future.result(timeout=20))
    assert read.observation is not None
    assert read.observation.classification == "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP"
    assert read.cleanup == CleanupOutcome.CLOSED and read.fault is None
    monkeypatch.setattr(sqlite3, "connect", CONNECT)
    assert classification(gate) == "PENDING"


@pytest.mark.parametrize("history", [10, 10000, 100000])
def test_complete_runtime_api_budgets_and_index_plans_remain_fixed_with_valid_history(
    gate: Path,
    history: int,
    probe_factory: Callable[..., Probe],
) -> None:
    template = encode_record(attestation())
    with closing(CONNECT(gate, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO identities VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    str(UUID(int=n + 100000)),
                    str(UUID(int=n + 1000000)),
                    *astuple(CONTEXT),
                    OWNER.run_id,
                    OWNER.fence_sha256,
                    "COMPLETED",
                )
                for n in range(history)
            ),
        )
        conn.executemany(
            "INSERT INTO completions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    CONTEXT.reference(str(UUID(int=n + 100000)), str(UUID(int=n + 1000000))),
                    str(UUID(int=n + 100000)),
                    str(UUID(int=n + 1000000)),
                    *astuple(CONTEXT),
                    "NORMAL_ATTESTATION_V1",
                    template.replace(OWNER.operation.encode(), str(UUID(int=n + 100000)).encode()).replace(
                        OWNER.launch.encode(), str(UUID(int=n + 1000000)).encode()
                    ),
                )
                for n in range(history)
            ),
        )
        conn.execute("COMMIT")
        for query, value, index in (
            (IDENTITY_QUERY, OWNER.operation, "sqlite_autoindex_identities_1"),
            (COMPLETION_OPERATION_QUERY, OWNER.operation, "sqlite_autoindex_completions_2"),
            (COMPLETION_LAUNCH_QUERY, OWNER.launch, "sqlite_autoindex_completions_3"),
            (
                "SELECT 1 FROM identities INDEXED BY sqlite_autoindex_identities_1 WHERE operation=? LIMIT 2",
                OWNER.operation,
                "sqlite_autoindex_identities_1",
            ),
            (
                "SELECT 1 FROM identities INDEXED BY sqlite_autoindex_identities_2 WHERE launch=? LIMIT 2",
                OWNER.launch,
                "sqlite_autoindex_identities_2",
            ),
            (
                "SELECT 1 FROM completions INDEXED BY sqlite_autoindex_completions_2 WHERE operation=? LIMIT 2",
                OWNER.operation,
                "sqlite_autoindex_completions_2",
            ),
            (
                "SELECT 1 FROM completions INDEXED BY sqlite_autoindex_completions_3 WHERE launch=? LIMIT 2",
                OWNER.launch,
                "sqlite_autoindex_completions_3",
            ),
        ):
            plans = conn.execute("EXPLAIN QUERY PLAN " + query, (value,)).fetchall()
            assert len(plans) == 1 and "SEARCH" in plans[0][3] and index in plans[0][3]
    probe = probe_factory()
    for action, maximum in (("observe", 14), ("begin", 20), ("complete", 17)):
        start = len(probe.calls)
        result = (
            store.observe(gate, context=CONTEXT)
            if action == "observe"
            else (
                store.begin_operation(gate, context=CONTEXT, owner=OWNER)
                if action == "begin"
                else store.complete_operation(gate, context=CONTEXT, attestation=attestation())
            )
        )
        assert result.fault is None and result.refusal is None
        assert len(probe.calls) - start == maximum
    next_owner = replace(OWNER, operation=str(UUID(int=40)), launch=str(UUID(int=41)))
    committed(store.begin_operation(gate, context=CONTEXT, owner=next_owner))
    start = len(probe.calls)
    committed(
        store.lock_fault(
            gate,
            context=CONTEXT,
            selector=RuntimeSelector(next_owner),
            expected_state="PENDING",
            reason="RESOURCE_UNCERTAIN",
        )
    )
    assert len(probe.calls) - start == 16


@pytest.mark.parametrize("stage", ["create", "header", "directory1", "directory2"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_file_descriptor_close_and_directory_publication_knowledge_are_independent(
    gate: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    when: str,
) -> None:
    original_open, original_close = os.open, os.close
    path = gate if stage == "header" else gate.with_name("new.sqlite")
    watched: int | None = None
    directory_count = 0
    injected = False

    def open_fd(name: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal watched, directory_count
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        matches = (stage == "create" and flags & os.O_CREAT) or (stage == "header" and name == path)
        if flags & os.O_DIRECTORY:
            directory_count += 1
            matches = stage == f"directory{directory_count}"
        if matches:
            watched = fd
        return fd

    def close_fd(fd: int) -> None:
        nonlocal injected
        matches = fd == watched and not injected
        if matches:
            injected = True
            if when == "before":
                raise OSError("injected before close")
        original_close(fd)
        if matches and when == "after":
            raise OSError("injected after close")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_fd)
        patch.setattr(os, "close", close_fd)
        try:
            result = (
                store.observe(path, context=CONTEXT)
                if stage == "header"
                else store.provision_fixture(path, context=CONTEXT)
            )
        finally:
            if injected and when == "before" and watched is not None:
                original_close(watched)
    assert injected
    assert result.fault == store.Fault.CLOSE_FAILURE and result.cleanup == CleanupOutcome.UNCERTAIN
    if isinstance(result, store.ReadResult):
        assert result.observation is None
    else:
        assert result.observed_created_path
        assert result.mutation == (
            MutationOutcome.COMMITTED if stage == "directory2" else MutationOutcome.NOT_ATTEMPTED
        )
        assert result.path_publication == (
            store.Publication.CONFIRMED if stage == "directory2" else store.Publication.UNKNOWN
        )
        assert store.provision_fixture(path, context=CONTEXT).refusal == store.Refusal.PREEXISTING_PATH
