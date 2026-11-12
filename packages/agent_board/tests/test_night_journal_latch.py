import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from agent_board import night_state
from agent_board.night import journal_latch as api
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
    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path = directory / "state" / "journal.sqlite3"
        self.now = ClockReading(str(uuid4()), 100.0, 1000.0)
        self.grant = Grant(str(uuid4()), "f" * 43, "pi@dev", 2001, self.now.boot_id, 900.0, 1900.0)
        journal = LocalJournal(self.path, box="dev", principals={"pi@dev": 2001}, clock=lambda: self.now)
        journal.reserve(self.grant)
        journal.claim_start(self.grant.run_id, self.grant.fence)
        monkeypatch.setattr(api, "clock_reading", lambda: self.now)

    def latch(self, *, missing: bool = False, wrong: bool = False) -> api.LatchResult:
        return api.latch(
            self.path,
            run_id=str(UUID(int=1)) if missing else self.grant.run_id,
            fence="e" * 43 if wrong else self.grant.fence,
        )

    def row(self) -> tuple[Any, ...]:
        with sqlite3.connect(self.path) as conn:
            return tuple(
                conn.execute(
                    "SELECT state,launch_claimed,stop_requested,uncertain,reason FROM runs WHERE run_id=?",
                    (self.grant.run_id,),
                ).fetchone()
            )


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> State:
    return State(tmp_path, monkeypatch)


class TrackingConnection(sqlite3.Connection):
    statements: list[str]
    counts: dict[str, int]
    faults: list[tuple[str, int, str]]
    closes: int
    on_close_open: list[bool]

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        self.statements.append(sql)
        operation = sql.split()[0].upper()
        self.counts[operation] = self.counts.get(operation, 0) + 1
        ordinal = self.counts[operation]
        if (operation, ordinal, "before") in self.faults:
            raise sqlite3.OperationalError("injected before underlying operation")
        result = super().execute(sql, parameters)
        if (operation, ordinal, "after") in self.faults:
            raise sqlite3.OperationalError("injected after underlying operation")
        return result

    def close(self) -> None:
        self.closes += 1
        self.on_close_open.append(self.in_transaction)
        if ("CLOSE", self.closes, "before") in self.faults:
            raise sqlite3.OperationalError("injected before close")
        super().close()
        if ("CLOSE", self.closes, "after") in self.faults:
            raise sqlite3.OperationalError("injected after close")


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[TrackingConnection]]:
    original = sqlite3.connect
    opened: list[TrackingConnection] = []

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if "mode=rw" not in str(args[0]):
            conn = original(*args, **kwargs)
            assert isinstance(conn, sqlite3.Connection)
            return conn
        assert kwargs["timeout"] == 0 and "immutable" not in args[0]
        conn = original(*args, **kwargs, factory=TrackingConnection)
        conn.statements, conn.counts, conn.faults, conn.on_close_open = [], {}, list(faults), []
        conn.closes = 0
        opened.append(conn)
        return conn

    faults: list[tuple[str, int, str]] = []
    monkeypatch.setattr(sqlite3, "connect", connect)
    opened_faults[id(opened)] = faults
    yield opened
    opened_faults.pop(id(opened))
    for conn in opened:
        sqlite3.Connection.close(conn)


opened_faults: dict[int, list[tuple[str, int, str]]] = {}


def fresh_clocks(
    state: State, monkeypatch: pytest.MonkeyPatch, *, first_expired: bool = False
) -> list[ClockReading]:
    expired = ClockReading(state.now.boot_id, 900.0, 1900.0)
    calls: list[ClockReading] = []

    def clock() -> ClockReading:
        reading = expired if first_expired or calls else state.now
        calls.append(reading)
        return reading

    monkeypatch.setattr(api, "clock_reading", clock)
    return calls


def test_success_and_idempotence(state: State, tracked: list[TrackingConnection]) -> None:
    found = state.latch()
    assert found.primary == api.MutationOutcome.COMMITTED and found.cleanup == api.CleanupOutcome.CLOSED
    assert found.primary_changed is True and found.latch_changed is True and found.record is not None
    assert found.record.state == "stopping" and found.record.stop_requested
    assert found.expiry == api.MutationOutcome.NOT_ATTEMPTED and found.fault is None
    assert len(tracked[0].statements) == 12 and tracked[0].closes == 1
    repeated = state.latch()
    assert repeated.primary_changed is False and repeated.latch_changed is False
    assert repeated.record == found.record


@pytest.mark.parametrize("missing", [False, True])
def test_domain_refusal_commits_fresh_second_expiry(
    state: State, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackingConnection], missing: bool
) -> None:
    calls = fresh_clocks(state, monkeypatch)
    found = state.latch(missing=missing, wrong=not missing)
    assert len(calls) == 2 and calls[0] != calls[1]
    assert found.primary == api.MutationOutcome.ROLLED_BACK and found.primary_changed is False
    assert found.expiry == api.MutationOutcome.COMMITTED and found.expiry_changed is True
    assert found.domain == "stale_fence" and found.fault is None and found.record is None
    assert state.row() == ("quarantined", 1, 1, 0, "deadline_passed")
    assert len(tracked[0].statements) <= 20 and tracked[0].counts["BEGIN"] == 2


@pytest.mark.parametrize("state_name", ["reserved", "starting", "stopping", "quarantined", "stopped"])
@pytest.mark.parametrize("claimed", [0, 1])
@pytest.mark.parametrize("uncertain", [0, 1])
@pytest.mark.parametrize("expiry", ["none", "boot", "boottime", "utc"])
def test_expiry_matches_legacy_for_valid_states(
    state: State, state_name: str, claimed: int, uncertain: int, expiry: str
) -> None:
    with sqlite3.connect(state.path) as conn:
        conn.execute("UPDATE runs SET state=?,launch_claimed=?,uncertain=?", (state_name, claimed, uncertain))
    if expiry == "boot":
        state.now = ClockReading(str(uuid4()), 100.0, 1000.0)
    elif expiry == "boottime":
        state.now = ClockReading(state.now.boot_id, 900.0, 1000.0)
    elif expiry == "utc":
        state.now = ClockReading(state.now.boot_id, 100.0, 1900.0)
    legacy = state.path.with_name("legacy.sqlite3")
    with sqlite3.connect(state.path) as source, sqlite3.connect(legacy) as target:
        source.backup(target)
        LocalJournal._refresh(target, state.now)
    with sqlite3.connect(legacy) as conn:
        expected = tuple(
            conn.execute("SELECT state,launch_claimed,stop_requested,uncertain,reason FROM runs").fetchone()
        )
    result = state.latch(missing=True)
    assert result.domain == "stale_fence" and result.fault is None
    assert state.row() == expected


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("followup", [False, True])
def test_commit_fault_keeps_unknown_separate_from_observed_state(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    tracked: list[TrackingConnection],
    timing: str,
    followup: bool,
) -> None:
    fresh_clocks(state, monkeypatch)
    opened_faults[id(tracked)].append(("COMMIT", 1, timing))
    result = state.latch(wrong=followup)
    outcome = result.expiry if followup else result.primary
    changed = result.expiry_changed if followup else result.primary_changed
    assert outcome == api.MutationOutcome.UNKNOWN and changed is None
    assert result.cleanup == api.CleanupOutcome.CLOSED and result.record is None and result.fault is not None
    assert result.latch_changed is None
    assert state.row()[0] == (
        ("quarantined" if followup else "stopping") if timing == "after" else "starting"
    )
    assert tracked[0].closes == 1 and len(tracked[0].statements) <= 21


@pytest.mark.parametrize("timing", ["before", "after"])
def test_first_rollback_fault_never_starts_followup(
    state: State, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackingConnection], timing: str
) -> None:
    calls = fresh_clocks(state, monkeypatch, first_expired=True)
    opened_faults[id(tracked)].append(("ROLLBACK", 1, timing))
    result = state.latch(wrong=True)
    assert result.primary == api.MutationOutcome.ROLLED_BACK
    assert result.expiry == api.MutationOutcome.NOT_ATTEMPTED and len(calls) == 1
    assert result.cleanup == api.CleanupOutcome.CLOSED and result.fault is not None
    assert tracked[0].counts["BEGIN"] == 1 and tracked[0].counts["ROLLBACK"] == 1
    assert state.row()[0] == "starting"


@pytest.mark.parametrize("timing", ["before", "after"])
def test_cleanup_rollback_fault_does_not_resolve_commit_ambiguity(
    state: State, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackingConnection], timing: str
) -> None:
    fresh_clocks(state, monkeypatch)
    opened_faults[id(tracked)].extend([("COMMIT", 1, "before"), ("ROLLBACK", 2, timing)])
    result = state.latch(wrong=True)
    assert result.expiry == api.MutationOutcome.UNKNOWN and result.expiry_changed is None
    assert result.cleanup == api.CleanupOutcome.CLOSED and result.fault == "rollback_failure"
    assert state.row()[0] == "starting"
    assert tracked[0].counts["BEGIN"] == 2 and tracked[0].closes == 1


@pytest.mark.parametrize("timing", ["before", "after"])
def test_successful_commit_survives_close_failure(
    state: State, tracked: list[TrackingConnection], timing: str
) -> None:
    opened_faults[id(tracked)].append(("CLOSE", 1, timing))
    result = state.latch()
    assert result.primary == api.MutationOutcome.COMMITTED and result.primary_changed is True
    assert result.latch_changed is True and result.cleanup == api.CleanupOutcome.UNCERTAIN
    assert result.record is None and result.fault == "close_failure"
    assert state.row()[0] == "stopping" and tracked[0].closes == 1


@pytest.mark.parametrize("sql", ["DROP INDEX one_active_run", "PRAGMA user_version=1"])
def test_validation_failure_after_begin_tracks_cleanup_without_body(
    state: State, tracked: list[TrackingConnection], sql: str
) -> None:
    with sqlite3.connect(state.path) as conn:
        conn.execute(sql)
    result = state.latch()
    assert result.primary == api.MutationOutcome.NOT_ATTEMPTED and result.primary_changed is False
    assert result.cleanup == api.CleanupOutcome.CLOSED and result.domain is None
    assert tracked[0].counts["BEGIN"] == 1 and tracked[0].counts["ROLLBACK"] == 1
    assert tracked[0].closes == 1


@pytest.mark.parametrize("timing", ["before", "after"])
def test_begin_fault_never_enters_body(state: State, tracked: list[TrackingConnection], timing: str) -> None:
    opened_faults[id(tracked)].append(("BEGIN", 1, timing))
    result = state.latch()
    assert result.primary == api.MutationOutcome.NOT_ATTEMPTED and result.cleanup == api.CleanupOutcome.CLOSED
    assert result.domain is None and tracked[0].closes == 1
    assert tracked[0].on_close_open == [timing == "after"]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE runs SET reason=CAST(X'FF' AS TEXT)",
        "UPDATE runs SET principal='invalid'",
        "UPDATE runs SET fence='broken'",
    ],
)
def test_corrupt_target_does_not_trigger_domain_followup(
    state: State, tracked: list[TrackingConnection], sql: str
) -> None:
    with sqlite3.connect(state.path) as conn:
        conn.execute(sql)
    result = state.latch(wrong=True)
    assert result.domain is None and result.expiry == api.MutationOutcome.NOT_ATTEMPTED
    assert result.primary == api.MutationOutcome.ROLLED_BACK and result.fault is not None
    assert tracked[0].counts["BEGIN"] == 1


def test_delete_reader_blocks_commit_without_retry(state: State, tracked: list[TrackingConnection]) -> None:
    reader = sqlite3.connect(state.path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM runs").fetchall()
        started = time.monotonic()
        result = state.latch()
        assert time.monotonic() - started < 0.5
    finally:
        reader.close()
    assert result.primary == api.MutationOutcome.UNKNOWN and result.primary_changed is None
    assert result.cleanup == api.CleanupOutcome.CLOSED and state.row()[0] == "starting"
    assert tracked[0].counts["COMMIT"] == 1 and tracked[0].counts["ROLLBACK"] == 1


@pytest.mark.parametrize("mode", ["IMMEDIATE", "EXCLUSIVE"])
def test_external_writer_refuses_without_second_transaction(
    state: State, tracked: list[TrackingConnection], mode: str
) -> None:
    writer = sqlite3.connect(state.path)
    try:
        writer.execute(f"BEGIN {mode}")
        result = state.latch()
    finally:
        writer.close()
    assert result.primary == api.MutationOutcome.NOT_ATTEMPTED and result.domain is None
    assert result.expiry == api.MutationOutcome.NOT_ATTEMPTED and result.cleanup == api.CleanupOutcome.CLOSED
    assert tracked[0].counts.get("BEGIN", 0) <= 1


@pytest.mark.parametrize("history", [10, 10000, 100000])
def test_history_scale_keeps_index_searches_and_statement_budget(
    state: State, tracked: list[TrackingConnection], history: int
) -> None:
    with sqlite3.connect(state.path) as conn:
        conn.executemany(
            "INSERT INTO runs(run_id,fence,principal,uid,boot_id,deadline_boottime,deadline_utc,state) VALUES(?,?,?,?,?,?,?,'stopped')",
            (
                (str(UUID(int=i + 1)), "f" * 43, "pi@dev", 2001, state.now.boot_id, 900.0, 1900.0)
                for i in range(history)
            ),
        )
        active = conn.execute("EXPLAIN QUERY PLAN " + api.ACTIVE_QUERY).fetchall()
        target = conn.execute("EXPLAIN QUERY PLAN " + api.TARGET_QUERY, (state.grant.run_id,)).fetchall()
        assert all("SEARCH" in row[3] and "SCAN" not in row[3] for row in active + target)
        assert "one_active_run" in active[0][3] and "sqlite_autoindex_runs_1" in target[0][3]
    result = state.latch()
    assert result.primary == api.MutationOutcome.COMMITTED and len(tracked[0].statements) == 12


def test_missing_file_is_not_created(state: State) -> None:
    missing = state.path.with_name("missing.sqlite3")
    result = api.latch(missing, run_id=state.grant.run_id, fence=state.grant.fence)
    assert not missing.exists() and result.primary == api.MutationOutcome.NOT_ATTEMPTED
    assert result.cleanup == api.CleanupOutcome.CLOSED


@pytest.mark.parametrize("operation,ordinal", [("BEGIN", 2), ("PRAGMA", 7), ("UPDATE", 1)])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_followup_failures_preserve_truthful_outcomes(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    tracked: list[TrackingConnection],
    operation: str,
    ordinal: int,
    timing: str,
) -> None:
    fresh_clocks(state, monkeypatch)
    opened_faults[id(tracked)].append((operation, ordinal, timing))
    result = state.latch(wrong=True)
    assert result.primary == api.MutationOutcome.ROLLED_BACK and result.cleanup == api.CleanupOutcome.CLOSED
    assert result.expiry == (
        api.MutationOutcome.ROLLED_BACK if operation == "UPDATE" else api.MutationOutcome.NOT_ATTEMPTED
    )
    assert result.expiry_changed is False and result.fault is not None and result.record is None
    assert state.row()[0] == "starting" and tracked[0].counts["BEGIN"] == 2
    assert len(tracked[0].statements) <= 21


@pytest.mark.parametrize("timing", ["before", "after"])
def test_followup_commit_survives_close_failure(
    state: State, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackingConnection], timing: str
) -> None:
    fresh_clocks(state, monkeypatch)
    opened_faults[id(tracked)].append(("CLOSE", 1, timing))
    result = state.latch(wrong=True)
    assert (
        result.primary == api.MutationOutcome.ROLLED_BACK and result.expiry == api.MutationOutcome.COMMITTED
    )
    assert result.expiry_changed is True and result.cleanup == api.CleanupOutcome.UNCERTAIN
    assert result.fault == "close_failure" and result.record is None
    assert state.row()[0] == "quarantined"


def test_second_schema_validation_is_inside_second_transaction(
    state: State, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackingConnection]
) -> None:
    fresh_clocks(state, monkeypatch, first_expired=True)
    original = TrackingConnection.execute

    def execute(conn: TrackingConnection, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        result = original(conn, sql, parameters)
        if sql == "ROLLBACK" and conn.counts["ROLLBACK"] == 1:
            with sqlite3.connect(state.path) as actor:
                actor.execute("PRAGMA user_version=1")
        return result

    monkeypatch.setattr(TrackingConnection, "execute", execute)
    result = state.latch(wrong=True)
    assert result.primary == api.MutationOutcome.ROLLED_BACK
    assert result.expiry == api.MutationOutcome.NOT_ATTEMPTED and result.cleanup == api.CleanupOutcome.CLOSED
    assert result.fault == "validation_or_path_failure" and state.row()[0] == "starting"
    assert tracked[0].counts["BEGIN"] == 2 and tracked[0].counts["ROLLBACK"] == 2


def test_invalid_second_clock_does_not_commit_expiry(
    state: State, monkeypatch: pytest.MonkeyPatch, tracked: list[TrackingConnection]
) -> None:
    samples = iter([state.now, None])
    monkeypatch.setattr(api, "clock_reading", lambda: next(samples))
    result = state.latch(wrong=True)
    assert (
        result.primary == api.MutationOutcome.ROLLED_BACK and result.expiry == api.MutationOutcome.ROLLED_BACK
    )
    assert result.expiry_changed is False and result.fault == "validation_or_path_failure"
    assert state.row()[0] == "starting" and tracked[0].counts.get("COMMIT", 0) == 0


def test_corrupt_historical_target_is_not_a_stale_fence_domain(
    state: State, tracked: list[TrackingConnection]
) -> None:
    target = str(UUID(int=2))
    with sqlite3.connect(state.path) as conn:
        conn.execute(
            "INSERT INTO runs(run_id,fence,principal,uid,boot_id,deadline_boottime,deadline_utc,state) VALUES(?,?,'invalid',2001,?,900,1900,'stopped')",
            (target, state.grant.fence, state.now.boot_id),
        )
    result = api.latch(state.path, run_id=target, fence="e" * 43)
    assert result.domain is None and result.expiry == api.MutationOutcome.NOT_ATTEMPTED
    assert result.primary == api.MutationOutcome.ROLLED_BACK and result.fault is not None
    assert tracked[0].counts["BEGIN"] == 1


@pytest.mark.parametrize("unsafe", ["symlink", "shared"])
def test_unsafe_file_is_refused_before_connect(
    state: State, tracked: list[TrackingConnection], unsafe: str
) -> None:
    path = state.path
    if unsafe == "symlink":
        path = state.path.with_name("link.sqlite3")
        path.symlink_to(state.path)
    else:
        path.chmod(0o644)
    result = api.latch(path, run_id=state.grant.run_id, fence=state.grant.fence)
    assert result.primary == api.MutationOutcome.NOT_ATTEMPTED and result.fault is not None
    assert tracked == [] and result.cleanup == api.CleanupOutcome.CLOSED


def test_invalid_selector_does_not_open_or_refresh(state: State, tracked: list[TrackingConnection]) -> None:
    result = api.latch(state.path, run_id="invalid", fence=state.grant.fence)
    assert result.fault == "invalid_selector" and result.domain is None
    assert result.primary == api.MutationOutcome.NOT_ATTEMPTED and tracked == []
