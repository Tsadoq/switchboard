import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night_inspection import Binding, EmptyObservation
from agent_board.night_state import ClockReading, Grant, LocalJournal, StateError


class Clock:
    def __init__(self) -> None:
        self.value = ClockReading(str(uuid4()), 100.0, 1000.0)

    def __call__(self) -> ClockReading:
        return self.value


@pytest.fixture(autouse=True)
def protected_fixture_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def metadata(path: Path) -> os.stat_result:
        result = path.lstat()
        if path in tmp_path.parents:
            values = list(result)
            values[0] = 0o40755
            values[4] = 0
            return os.stat_result(values)
        return result

    monkeypatch.setattr(night_state, "_ancestor_stat", metadata, raising=False)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def journal(tmp_path: Path, clock: Clock) -> LocalJournal:
    return LocalJournal(
        tmp_path / "state" / "runs.db",
        box="dev",
        principals={"pi@dev": 1000, "claude@dev": 1000},
        clock=clock,
    )


def grant(clock: Clock, principal: str = "pi@dev", **changes: object) -> Grant:
    values: dict[str, Any] = {
        "run_id": str(uuid4()),
        "fence": "a" * 43,
        "principal": principal,
        "uid": 1000,
        "boot_id": clock().boot_id,
        "deadline_boottime": 1300.0,
        "deadline_utc": 2200.0,
    }
    values.update(changes)
    return Grant(**values)


def observation(original: Grant, **changes: Any) -> EmptyObservation:
    values: dict[str, Any] = {
        "run_id": original.run_id,
        "uid": original.uid,
        "boot_id": original.boot_id,
        "invocation_id": "a" * 32,
    } | changes
    return EmptyObservation(Binding(**values), True, True)


def test_settled_release_is_fenced_idempotent_and_allows_one_new_run(tmp_path: Path, clock: Clock) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    assert store.claim_start(original.run_id, original.fence)
    store.request_stop(original.run_id, original.fence)
    proof = observation(original)
    result = store.release_settled(original.run_id, original.fence, observation=proof)
    assert result.state == "stopped" and result.reason == "server_settled" and result.launch_claimed
    assert store.release_settled(original.run_id, original.fence, observation=proof) == result
    assert not store.claim_start(original.run_id, original.fence)
    assert journal(tmp_path, clock).active() == ()
    assert store.reserve(grant(clock)).created


@pytest.mark.parametrize("change", [{"run_id": str(uuid4())}, {"uid": 1001}, {"boot_id": str(uuid4())}])
def test_settled_release_rejects_wrong_physical_binding(
    tmp_path: Path, clock: Clock, change: dict[str, Any]
) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    store.claim_start(original.run_id, original.fence)
    store.request_stop(original.run_id, original.fence)
    with pytest.raises(StateError):
        store.release_settled(original.run_id, original.fence, observation=observation(original, **change))
    assert len(store.active()) == 1


@pytest.mark.parametrize("phase", ["reserved", "starting", "wrong_fence", "changed_boot"])
def test_settled_release_rejects_unsafe_lifecycle_or_identity(
    tmp_path: Path, clock: Clock, phase: str
) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    if phase != "reserved":
        store.claim_start(original.run_id, original.fence)
    if phase not in ("reserved", "starting"):
        store.request_stop(original.run_id, original.fence)
    if phase == "changed_boot":
        clock.value = ClockReading(str(uuid4()), 10, clock().utc + 1)
    with pytest.raises(StateError):
        store.release_settled(
            original.run_id,
            "b" * 43 if phase == "wrong_fence" else original.fence,
            observation=observation(original),
        )
    assert len(store.active()) == 1


def test_settled_release_can_clear_proven_uncertainty_without_erasing_history(
    tmp_path: Path, clock: Clock
) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    store.claim_start(original.run_id, original.fence)
    store.quarantine(original.run_id, original.fence, "proof_incomplete")
    result = store.release_settled(original.run_id, original.fence, observation=observation(original))
    assert result.state == "stopped" and result.uncertain and result.stop_requested
    assert store.active() == ()


@pytest.mark.parametrize("proof", [True, False, None])
def test_settled_release_never_accepts_a_stopped_boolean(tmp_path: Path, clock: Clock, proof: Any) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    store.claim_start(original.run_id, original.fence)
    store.request_stop(original.run_id, original.fence)
    with pytest.raises(StateError, match="invalid_stop_observation"):
        store.release_settled(original.run_id, original.fence, observation=proof)
    assert len(store.active()) == 1


def test_concurrent_server_grants_cannot_double_book_the_box(tmp_path: Path, clock: Clock) -> None:
    store = journal(tmp_path, clock)

    def reserve(index: int) -> bool:
        try:
            return store.reserve(grant(clock, "pi@dev" if index % 2 else "claude@dev")).created
        except StateError as error:
            assert str(error) == "capacity"
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(reserve, range(16))).count(True) == 1
    assert len(store.active()) == 1
    assert (tmp_path / "state" / "runs.db").stat().st_mode & 0o777 == 0o600


def test_retries_do_not_renew_or_issue_a_second_launch(tmp_path: Path, clock: Clock) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    first = store.reserve(original)
    clock.value = ClockReading(clock().boot_id, 200.0, 1100.0)
    repeated = store.reserve(
        grant(clock, run_id=original.run_id, deadline_boottime=1400.0, deadline_utc=2300.0)
    )
    assert not repeated.created and repeated.record == first.record
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim_start(original.run_id, original.fence), range(8)))
    assert claims.count(True) == 1
    assert not journal(tmp_path, clock).claim_start(original.run_id, original.fence)


def test_fence_or_identity_substitution_cannot_change_a_run(tmp_path: Path, clock: Clock) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    first = store.reserve(original)
    for changes in ({"fence": "b" * 43}, {"principal": "claude@dev"}, {"uid": 1001}):
        with pytest.raises(StateError):
            store.reserve(grant(clock, run_id=original.run_id, **changes))
    with pytest.raises(StateError, match="stale_fence"):
        store.request_stop(original.run_id, "b" * 43)
    assert store.active() == (first.record,)
    assert original.fence not in repr(first)


def test_crash_after_launch_claim_retains_capacity_and_cannot_be_cancelled_as_unlaunched(
    tmp_path: Path, clock: Clock
) -> None:
    original = grant(clock)
    store = journal(tmp_path, clock)
    store.reserve(original)
    assert store.claim_start(original.run_id, original.fence)
    restarted = journal(tmp_path, clock)
    stopped = restarted.request_stop(original.run_id, original.fence)
    assert stopped.state == "stopping" and stopped.launch_claimed
    assert len(restarted.active()) == 1
    with pytest.raises(StateError, match="capacity"):
        restarted.reserve(grant(clock, "claude@dev"))


@pytest.mark.parametrize("changed_boot", [False, True])
def test_expiry_or_boot_change_never_releases_uncertain_work(
    tmp_path: Path, clock: Clock, changed_boot: bool
) -> None:
    original = grant(clock)
    store = journal(tmp_path, clock)
    store.reserve(original)
    assert store.claim_start(original.run_id, original.fence)
    clock.value = ClockReading(str(uuid4()) if changed_boot else clock().boot_id, 1400.0, 2300.0)
    restarted = journal(tmp_path, clock)
    assert restarted.active()[0].state == "quarantined"
    with pytest.raises(StateError, match="capacity"):
        restarted.reserve(grant(clock, deadline_boottime=2400.0, deadline_utc=3300.0))


def test_unclaimed_intent_can_be_cancelled_but_never_replayed(tmp_path: Path, clock: Clock) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    cancelled = store.request_stop(original.run_id, original.fence)
    assert cancelled.state == "stopped" and not cancelled.launch_claimed
    assert store.active() == ()
    assert not store.claim_start(original.run_id, original.fence)
    assert not store.reserve(original).created
    assert store.reserve(grant(clock)).created


def test_stop_and_launch_race_never_releases_a_claimed_start(tmp_path: Path, clock: Clock) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        for index in range(12):
            store = journal(tmp_path / str(index), clock)
            original = grant(clock)
            store.reserve(original)
            start = pool.submit(store.claim_start, original.run_id, original.fence)
            stop = pool.submit(store.request_stop, original.run_id, original.fence)
            claimed, record = start.result(), stop.result()
            if claimed:
                assert record.launch_claimed and record.state == "stopping"
                assert len(store.active()) == 1
            else:
                assert not record.launch_claimed and record.state == "stopped"
                assert store.active() == ()


def test_repeated_uncertainty_retains_the_original_cause_and_slot(tmp_path: Path, clock: Clock) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    assert store.claim_start(original.run_id, original.fence)
    first = store.quarantine(original.run_id, original.fence, "launch_uncertain")
    assert first.state == "quarantined" and first.stop_requested
    assert store.quarantine(original.run_id, original.fence, "proof_incomplete") == first
    assert store.request_stop(original.run_id, original.fence) == first
    assert not store.claim_start(original.run_id, original.fence)
    with pytest.raises(StateError, match="capacity"):
        store.reserve(grant(clock, "claude@dev"))


def test_oversized_integer_clocks_fail_as_invalid_input(clock: Clock) -> None:
    with pytest.raises(StateError, match="invalid_clock"):
        ClockReading(clock().boot_id, 10**400, 1000)
    with pytest.raises(StateError, match="invalid_deadline"):
        grant(clock, deadline_utc=10**400)


@pytest.mark.parametrize("axis", ["boottime", "utc"])
def test_each_deadline_independently_holds_the_gate_on_expiry(
    tmp_path: Path, clock: Clock, axis: str
) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    assert store.claim_start(original.run_id, original.fence)
    clock.value = ClockReading(
        clock().boot_id, 1300 if axis == "boottime" else 100, 2200 if axis == "utc" else 900
    )
    record = store.active()[0]
    assert record.state == "quarantined" and record.stop_requested
    assert store.request_stop(original.run_id, original.fence).state == "quarantined"


def test_too_short_launch_is_not_claimed_and_explicit_cancellation_can_close_it(
    tmp_path: Path, clock: Clock
) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    clock.value = ClockReading(clock().boot_id, 1295, 2195)
    assert not store.claim_start(original.run_id, original.fence)
    assert store.active() == ()
    stopped = store.request_stop(original.run_id, original.fence)
    assert stopped.reason == "never_claimed_insufficient_time" and stopped.state == "stopped"
    assert store.active() == ()


def test_corrupt_and_future_journals_fail_without_replacement(tmp_path: Path, clock: Clock) -> None:
    store = journal(tmp_path, clock)
    path = tmp_path / "state" / "runs.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 999")
    with pytest.raises(StateError, match="unsupported_state_version"):
        store.active()
    with pytest.raises(StateError, match="unsupported_state_version"):
        journal(tmp_path, clock)
    path.write_bytes(b"not a database")
    with pytest.raises(StateError, match="state_unavailable"):
        journal(tmp_path, clock)
    assert path.read_bytes() == b"not a database"


@pytest.mark.parametrize("unsafe", ["parent_mode", "file_mode", "symlink", "hardlink"])
def test_unsafe_state_paths_are_not_silently_repaired(tmp_path: Path, clock: Clock, unsafe: str) -> None:
    journal(tmp_path, clock)
    parent = tmp_path / "state"
    path = parent / "runs.db"
    if unsafe == "parent_mode":
        parent.chmod(0o755)
    elif unsafe == "file_mode":
        path.chmod(0o644)
    elif unsafe == "symlink":
        path.rename(parent / "original.db")
        path.symlink_to(parent / "original.db")
    else:
        os.link(path, parent / "alias.db")
    with pytest.raises((StateError, OSError)):
        journal(tmp_path, clock)


@pytest.mark.parametrize(
    "changes",
    [
        {"uid": 0},
        {"uid": True},
        {"principal": "pi@elsewhere"},
        {"principal": "verifier@dev"},
        {"deadline_boottime": float("nan")},
        {"deadline_utc": float("inf")},
        {"deadline_boottime": 106.0},
        {"deadline_utc": 1006.0},
        {"boot_id": str(uuid4())},
    ],
)
def test_invalid_or_expired_grants_never_occupy_capacity(
    tmp_path: Path, clock: Clock, changes: dict[str, Any]
) -> None:
    store = journal(tmp_path, clock)
    with pytest.raises(StateError):
        store.reserve(grant(clock, **changes))
    assert store.active() == ()


@pytest.mark.parametrize(
    "path",
    [Path("relative/runs.db"), Path("/var/lib/night/../other/runs.db"), Path("//var/lib/night/runs.db")],
)
def test_unnormalized_paths_fail_before_filesystem_access(
    path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("filesystem used for non-normalized path")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(night_state, "_ancestor_stat", forbidden)
    with pytest.raises(StateError, match="invalid_state_path"):
        LocalJournal(path, box="dev", principals={"pi@dev": 1000}, clock=clock)


def stored(tmp_path: Path, run_id: str) -> dict[str, Any]:
    with sqlite3.connect(tmp_path / "state" / "runs.db") as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("cause", ["boot_changed", "expired"])
def test_sweep_distinguishes_never_claimed_from_possible_work(
    tmp_path: Path, clock: Clock, claimed: bool, cause: str
) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    if claimed:
        assert store.claim_start(original.run_id, original.fence)
    clock.value = ClockReading(str(uuid4()) if cause == "boot_changed" else clock().boot_id, 1400, 2300)
    restarted = journal(tmp_path, clock)
    row = stored(tmp_path, original.run_id)
    assert row["state"] == ("quarantined" if claimed else "stopped")
    assert row["reason"] == (
        ("boot_changed" if cause == "boot_changed" else "deadline_passed")
        if claimed
        else "never_claimed_" + cause
    )
    assert bool(restarted.active()) == claimed
    assert not restarted.claim_start(original.run_id, original.fence)


@pytest.mark.parametrize("reason", ["launch_uncertain", "binding_conflict", "proof_incomplete"])
def test_explicit_uncertainty_latches_even_without_a_launch_claim(
    tmp_path: Path, clock: Clock, reason: str
) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    record = store.quarantine(original.run_id, original.fence, reason)
    assert record.uncertain and not record.launch_claimed
    assert store.request_stop(original.run_id, original.fence).state == "quarantined"
    clock.value = ClockReading(str(uuid4()), 1400, 2300)
    assert journal(tmp_path, clock).active()[0].reason == reason


@pytest.mark.parametrize("rejection", ["capacity", "stale_fence", "run_binding"])
def test_rejected_operations_still_commit_the_clock_sweep(
    tmp_path: Path, clock: Clock, rejection: str
) -> None:
    store = journal(tmp_path, clock)
    original = grant(clock)
    store.reserve(original)
    assert store.claim_start(original.run_id, original.fence)
    clock.value = ClockReading(clock().boot_id, 1400, 2300)
    with pytest.raises(StateError, match=rejection):
        if rejection == "capacity":
            store.reserve(grant(clock, deadline_boottime=2400, deadline_utc=3300))
        elif rejection == "stale_fence":
            store.request_stop(original.run_id, "b" * 43)
        else:
            store.reserve(grant(clock, "claude@dev", run_id=original.run_id))
    assert stored(tmp_path, original.run_id)["reason"] == "deadline_passed"
    assert stored(tmp_path, original.run_id)["state"] == "quarantined"


@pytest.mark.parametrize("unsafe", ["writable", "foreign_owner", "symlink"])
def test_unsafe_ancestors_are_rejected_before_creation(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    parent = tmp_path / "ancestor"
    parent.mkdir()
    if unsafe == "writable":
        parent.chmod(0o777)
    elif unsafe == "foreign_owner":
        original = night_state._ancestor_stat

        def foreign(path: Path) -> os.stat_result:
            values = list(original(path))
            if path == parent:
                values[4] = 999999
            return os.stat_result(values)

        monkeypatch.setattr(night_state, "_ancestor_stat", foreign)
    else:
        parent.rename(tmp_path / "original")
        parent.symlink_to(tmp_path / "original")
    with pytest.raises(StateError, match="unsafe_state_permissions"):
        journal(parent, clock)
    assert not (parent / "state").exists()


def test_permissions_are_rechecked_before_every_connection(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = journal(tmp_path, clock)
    tmp_path.chmod(0o777)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("SQLite opened before rejecting unsafe ancestors")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(StateError, match="unsafe_state_permissions"):
        store.active()


def test_parent_directory_is_synced_and_wal_is_refused(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[bool] = []
    real_sync = os.fsync

    def sync(fd: int) -> None:
        synced.append(os.path.isdir(f"/proc/self/fd/{fd}"))
        real_sync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    store = journal(tmp_path, clock)
    assert any(synced)
    with sqlite3.connect(tmp_path / "state" / "runs.db") as conn:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    with pytest.raises(StateError, match="unsupported_journal_mode"):
        store.active()
