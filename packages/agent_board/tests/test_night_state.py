import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board.runner.night_state import ClockReading, Grant, LocalJournal, StateError


class Clock:
    def __init__(self) -> None:
        self.value = ClockReading(str(uuid4()), 100.0, 1000.0)

    def __call__(self) -> ClockReading:
        return self.value


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
    assert store.active()[0].reason == "insufficient_time"
    assert store.request_stop(original.run_id, original.fence).state == "stopped"
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
