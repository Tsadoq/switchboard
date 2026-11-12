import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night import final_snapshot as api
from agent_board.night.assignment import NightPolicy, build_assignment, encode, parse_wakes
from agent_board.night.ledger import CaptureContext, Ledger, Pending
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import ClockReading, Grant, LocalJournal


@pytest.fixture(autouse=True)
def protected_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def metadata(path: Path) -> os.stat_result:
        result = path.lstat()
        if path in tmp_path.parents:
            values = list(result)
            values[0], values[4] = 0o40755, 0
            return os.stat_result(values)
        return result

    monkeypatch.setattr(night_state, "_ancestor_stat", metadata)


class State:
    def __init__(self, directory: Path) -> None:
        self.now = ClockReading(str(uuid4()), 100.0, 1000.0)
        self.grant = Grant(str(uuid4()), "f" * 43, "pi@dev", 2001, self.now.boot_id, 900.0, 1900.0)
        self.journal_path = directory / "state" / "journal.sqlite3"
        self.ledger_path = directory / "state" / "ledger.sqlite3"
        self.journal = LocalJournal(
            self.journal_path, box="dev", principals={"pi@dev": 2001}, clock=lambda: self.now
        )
        self.ledger = Ledger(self.ledger_path)
        self.journal.reserve(self.grant)
        self.journal.claim_start(self.grant.run_id, self.grant.fence)
        self.journal_id = str(uuid4())
        self.invocation = "b" * 32
        self.cgroup = unit_cgroup(f"switchboard-night-{self.grant.run_id}.service")
        self.ledger.record_binding(
            self.grant.run_id,
            uid=2001,
            boot_id=self.now.boot_id,
            invocation_id=self.invocation,
            cgroup=self.cgroup,
        )
        binding = Pending(str(uuid4()), "binding", self.grant.run_id, {})
        self.ledger.persist(binding)
        self.ledger.acknowledge(binding.receipt_id, journal_id=self.journal_id, counter=1)
        self.receipt_id = str(uuid4())
        self.payload = {
            "fence": self.grant.fence,
            "journal_id": self.journal_id,
            "counter": 1,
            "receipt_id": self.receipt_id,
            "boot_id": self.now.boot_id,
            "invocation_id": self.invocation,
            "cgroup": self.cgroup,
        }
        policy = NightPolicy("repo", "a" * 40, "claude", None, "read_only", 600)
        self.snapshot = {
            "run_id": self.grant.run_id,
            "capture_id": self.receipt_id,
            "principal": "pi@dev",
            "journal_id": self.journal_id,
            "counter": 2,
            "deadline": 1900.0,
            "wakes": [
                {
                    "id": 1,
                    "reason": "reply",
                    "source_ref": "post:1",
                    "summary": "test",
                    "summary_truncated": False,
                    "thread_id": 31,
                    "topic_id": 31,
                }
            ],
        }
        self.ledger.persist_capture(
            Pending(self.receipt_id, "capture", self.grant.run_id, self.payload),
            CaptureContext("pi@dev", 1850.0, policy),
        )
        assignment = build_assignment(
            run_id=self.grant.run_id,
            principal="pi@dev",
            wakes=parse_wakes(self.snapshot["wakes"]),
            policy=policy,
            deadline_utc=1850.0,
        )
        assert assignment is not None
        self.ledger.acknowledge(
            self.receipt_id,
            journal_id=self.journal_id,
            counter=2,
            capture=(json.dumps(self.snapshot), encode(assignment)),
        )

    def read(self, **changes: Any) -> api.LocalSnapshot:
        return api.final_snapshot(
            self.journal_path, self.ledger_path, expected=self.grant, now=self.now, grace=5.0, **changes
        )

    def change(self, sql: str, parameters: tuple[Any, ...] = (), *, journal: bool = False) -> None:
        with sqlite3.connect(self.journal_path if journal else self.ledger_path) as conn:
            conn.execute(sql, parameters)


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State(tmp_path)


def test_snapshot_returns_only_local_association_without_mutating(state: State) -> None:
    before = (state.journal_path.read_bytes(), state.ledger_path.read_bytes())
    found = state.read()
    assert found.record.run_id == state.grant.run_id
    assert found.capture_id == state.receipt_id
    assert found.journal_id == state.journal_id
    assert found.binding.invocation_id == state.invocation
    assert before == (state.journal_path.read_bytes(), state.ledger_path.read_bytes())


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE runs SET stop_requested=1",
        "UPDATE runs SET uncertain=1",
        "UPDATE runs SET state='stopping'",
        "UPDATE runs SET launch_claimed=0",
        "UPDATE runs SET uid=2002",
        "UPDATE runs SET deadline_utc=1901",
        "UPDATE runs SET reason='" + "x" * 257 + "'",
        "DROP INDEX one_active_run",
        "PRAGMA user_version=1",
    ],
)
def test_changed_or_unsafe_journal_refuses(state: State, sql: str) -> None:
    state.change(sql, journal=True)
    with pytest.raises(api.SnapshotRefused):
        state.read()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE bindings SET server_acked=0",
        "UPDATE bindings SET uid=2002",
        "UPDATE captures SET assignment=NULL",
        "UPDATE captures SET snapshot='{}'",
        "UPDATE receipts SET acked=0 WHERE action='capture'",
        "UPDATE receipts SET action='evidence' WHERE action='capture'",
        "UPDATE identity SET acked_counter=1",
        "DROP INDEX one_capture_per_run",
        "PRAGMA user_version=2",
    ],
)
def test_incomplete_or_mismatched_ledger_refuses(state: State, sql: str) -> None:
    state.change(sql)
    with pytest.raises(api.SnapshotRefused):
        state.read()


@pytest.mark.parametrize(
    "key,value",
    [
        ("fence", "x" * 43),
        ("journal_id", "11111111-1111-1111-1111-111111111111"),
        ("counter", True),
        ("counter", 2),
        ("receipt_id", "11111111-1111-1111-1111-111111111111"),
        ("boot_id", "11111111-1111-1111-1111-111111111111"),
        ("invocation_id", "c" * 32),
        ("cgroup", "/other"),
    ],
)
def test_receipt_association_must_be_complete(state: State, key: str, value: Any) -> None:
    state.payload[key] = value
    state.change("UPDATE receipts SET payload=? WHERE action='capture'", (json.dumps(state.payload),))
    with pytest.raises(api.SnapshotRefused):
        state.read()


@pytest.mark.parametrize("outcome,proof", [(None, None), ("aborted", "proof"), ("applied", "proof")])
def test_every_abort_link_refuses(state: State, outcome: str | None, proof: str | None) -> None:
    state.change(
        "INSERT INTO capture_aborts VALUES (?,?,?,?)", (state.receipt_id, str(uuid4()), outcome, proof)
    )
    with pytest.raises(api.SnapshotRefused, match="capture_aborted"):
        state.read()


def test_wal_commits_are_visible_to_a_later_snapshot(state: State) -> None:
    for path in (state.journal_path, state.ledger_path):
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert state.read().capture_id == state.receipt_id
    state.change("UPDATE runs SET stop_requested=1", journal=True)
    with pytest.raises(api.SnapshotRefused):
        state.read()


@pytest.mark.parametrize("journal", [True, False])
def test_delete_exclusive_writer_causes_immediate_busy_refusal(state: State, journal: bool) -> None:
    conn = sqlite3.connect(state.journal_path if journal else state.ledger_path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(api.SnapshotRefused):
            state.read()
        assert time.monotonic() - started < 0.5
    finally:
        conn.close()
    assert state.read().capture_id == state.receipt_id


@pytest.mark.parametrize("failure", [False, True])
def test_connections_close_and_cannot_write(
    state: State, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    original = sqlite3.connect
    opened = []
    closed = []
    traces: list[str] = []

    class Connection(sqlite3.Connection):
        def close(self) -> None:
            closed.append(self)
            super().close()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        assert kwargs["timeout"] == 0 and "mode=ro" in args[0] and "immutable" not in args[0]
        conn = original(*args, **kwargs, factory=Connection)
        conn.set_trace_callback(traces.append)
        opened.append(conn)
        return conn

    if failure:
        state.change("PRAGMA user_version=2")
    monkeypatch.setattr(sqlite3, "connect", connect)
    if failure:
        with pytest.raises(api.SnapshotRefused):
            state.read()
    else:
        state.read()
    assert len(opened) == 2 and set(opened) == set(closed)
    assert not any(statement.startswith(("UPDATE", "INSERT", "DELETE", "CREATE")) for statement in traces)
    assert traces.count("PRAGMA busy_timeout = 0") == 2
    assert traces.count("PRAGMA query_only = ON") == 2


def test_corrupt_database_refuses(state: State) -> None:
    state.ledger_path.write_bytes(b"not a database")
    with pytest.raises(api.SnapshotRefused):
        state.read()


@pytest.mark.parametrize("value", ["NaN", "{}", '{"counter":1,"counter":1}', '"' + "x" * 8193 + '"'])
def test_invalid_or_oversized_receipt_json_refuses(state: State, value: str) -> None:
    state.change("UPDATE receipts SET payload=? WHERE action='capture'", (value,))
    with pytest.raises(api.SnapshotRefused):
        state.read()
