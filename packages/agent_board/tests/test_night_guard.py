import os
import socket
import time
from pathlib import Path
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.controller import unit_name
from agent_board.night.guard import Guard, boottime_timer, systemd_notify
from agent_board.night.ledger import Ledger, Pending
from agent_board.night_state import ClockReading, Grant, LocalJournal

BOOT = str(uuid4())
UID = 1001
FENCE = "f" * 43
FIRST, SECOND = "a" * 32, "b" * 32


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


class Stopper:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    def stop(self, unit: str) -> None:
        self.stopped.append(unit)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.boottime = 100.0
        self.utc = 1_000.0
        self.journal = LocalJournal(
            tmp_path / "state" / "journal.sqlite3",
            box="huginn",
            principals={"pi@huginn": UID},
            clock=lambda: ClockReading(BOOT, self.boottime, self.utc),
        )
        self.ledger = Ledger(tmp_path / "state" / "ledger.sqlite3")
        self.stopper = Stopper()

    def guard(self, invocation: str = FIRST) -> Guard:
        return Guard(
            journal=self.journal,
            ledger=self.ledger,
            runs=self.stopper,
            stop_grace_seconds=5,
            invocation_id=invocation,
            boot_id=lambda: BOOT,
            boottime=lambda: self.boottime,
            utc=lambda: self.utc,
        )

    def launched_run(self, deadline_boottime: float = 700.0, deadline_utc: float = 1_600.0) -> str:
        run_id = str(uuid4())
        self.journal.reserve(Grant(run_id, FENCE, "pi@huginn", UID, BOOT, deadline_boottime, deadline_utc))
        self.journal.claim_start(run_id, FENCE)
        self.ledger.record_binding(run_id, uid=UID, boot_id=BOOT, invocation_id="c" * 32, cgroup="/x")
        receipt = Pending(str(uuid4()), "binding", run_id, {"receipt_id": "r"})
        self.ledger.persist(receipt)
        self.ledger.acknowledge(receipt.receipt_id, journal_id=str(uuid4()), counter=1)
        return run_id

    def ready(self, run_id: str, invocation: str, deadline_boottime: float = 700.0) -> bool:
        return self.ledger.ready(
            run_id,
            boot_id=BOOT,
            guard_invocation_id=invocation,
            deadline_boottime=deadline_boottime,
            deadline_utc=1_600.0,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_the_guard_arms_a_new_run_with_its_own_invocation_and_the_stored_deadlines(rig: Rig) -> None:
    run_id = rig.launched_run()

    rig.guard().tick()

    assert rig.ready(run_id, FIRST)
    assert rig.stopper.stopped == []


def test_a_restarted_guard_rearms_the_same_deadlines_and_the_old_arm_stops_counting(rig: Rig) -> None:
    run_id = rig.launched_run()
    rig.guard(FIRST).tick()
    rig.boottime += 300

    rig.guard(SECOND).tick()

    assert rig.ready(run_id, SECOND, deadline_boottime=700.0), "the restart did not keep the stored deadline"
    assert not rig.ready(run_id, FIRST), "the earlier guard's arm still satisfies readiness"


def test_a_launched_run_is_stopped_once_at_its_boot_deadline_minus_grace(rig: Rig) -> None:
    run_id = rig.launched_run()
    guard = rig.guard()
    guard.tick()
    rig.boottime = 695.0

    guard.tick()
    guard.tick()

    assert rig.stopper.stopped == [unit_name(run_id)], rig.stopper.stopped
    assert rig.journal.active()[0].state in ("stopping", "quarantined")


def test_the_utc_window_cutoff_also_stops_the_run(rig: Rig) -> None:
    run_id = rig.launched_run()
    rig.utc = 1_596.0

    rig.guard().tick()

    assert rig.stopper.stopped == [unit_name(run_id)], rig.stopper.stopped


def test_a_run_already_past_its_deadline_when_the_guard_starts_is_stopped_at_once(rig: Rig) -> None:
    run_id = rig.launched_run()
    rig.boottime = 900.0

    rig.guard().tick()

    assert rig.stopper.stopped == [unit_name(run_id)], rig.stopper.stopped


def test_a_reserved_run_that_never_launched_is_not_stopped(rig: Rig) -> None:
    rig.journal.reserve(Grant(str(uuid4()), FENCE, "pi@huginn", UID, BOOT, 700.0, 1_600.0))

    rig.guard().tick()

    assert rig.stopper.stopped == []


def test_the_guard_wakes_for_the_earliest_deadline_or_the_next_poll(rig: Rig) -> None:
    guard = rig.guard()
    idle = guard.next_wake()
    rig.launched_run(deadline_boottime=110.0)
    rig.boottime = 104.8

    assert idle == pytest.approx(101.0)
    assert guard.next_wake() == pytest.approx(105.0)


def test_the_boottime_timer_waits_until_an_absolute_deadline() -> None:
    wait_until = boottime_timer()
    target = time.clock_gettime(time.CLOCK_BOOTTIME) + 0.05

    wait_until(target)

    assert time.clock_gettime(time.CLOCK_BOOTTIME) >= target


def test_ready_is_sent_to_the_systemd_notify_socket(tmp_path: Path) -> None:
    address = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(address)

        systemd_notify({"NOTIFY_SOCKET": address})("READY=1")

        assert receiver.recv(64) == b"READY=1"


def test_the_guard_refuses_to_run_outside_a_notify_unit() -> None:
    with pytest.raises(RuntimeError, match="Type=notify"):
        systemd_notify({})
