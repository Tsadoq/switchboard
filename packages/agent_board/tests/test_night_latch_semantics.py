import dataclasses
import json
import os
import sqlite3
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night import latch
from agent_board.night.config import parse
from agent_board.night.latch import LATCHED_EXIT, REFUSED_EXIT, Outcome, latch_stop
from agent_board.night_state import ClockReading, Grant, LocalJournal, StateError

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_latch import _table
else:
    from test_night_latch import _table

BOOT = str(uuid4())
OTHER_BOOT = str(uuid4())
UID = 1001
FENCE = "f" * 43
WRONG_FENCE = "g" * 43
STOPPED = "LoadState=loaded\nActiveState=inactive\nJob=\nControlGroup=\n"
ACTIVE = "LoadState=loaded\nActiveState=active\nJob=\nControlGroup=/system.slice/guard.service\n"
CONFIG = parse(_table())
OUTCOME_KEYS = ["already", "latched", "no_run", "refused"]


class Clock:
    def __init__(self) -> None:
        self.reading = ClockReading(BOOT, 100.0, 1_000.0)

    def __call__(self) -> ClockReading:
        return self.reading

    def expire(self) -> None:
        self.reading = ClockReading(BOOT, 701.0, 1_000.0)

    def reboot(self) -> None:
        self.reading = ClockReading(OTHER_BOOT, 5.0, 1_000.0)


class ManagerModel:
    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}
        self.asked: list[str] = []

    def show(self, unit: str, properties: Sequence[str]) -> str:
        self.asked.append(unit)
        return self.outputs.get(unit, STOPPED)


class Rig:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "state" / "journal.sqlite3"
        self.clock = Clock()
        self.manager = ManagerModel({CONFIG.guard_unit: ACTIVE})
        self.asked_at_open: list[list[str]] = []

    def journal(self) -> LocalJournal:
        return LocalJournal(self.path, box="huginn", principals={"pi@huginn": UID}, clock=self.clock)

    def open_journal(self) -> LocalJournal:
        self.asked_at_open.append(list(self.manager.asked))
        return self.journal()

    def reserve(self, *, claim: bool) -> str:
        run_id = str(uuid4())
        journal = self.journal()
        journal.reserve(Grant(run_id, FENCE, "pi@huginn", UID, BOOT, 700.0, 1_600.0))
        if claim:
            assert journal.claim_start(run_id, FENCE)
        return run_id

    def latch(self, run_id: str | None, fence: str | None = FENCE, events: Any = None) -> Outcome:
        return latch_stop(
            CONFIG,
            self.open_journal,
            self.manager,
            run_id=run_id,
            fence=None if run_id is None else fence,
            events=events or _absent,
        )

    def row(self, run_id: str) -> dict[str, Any]:
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchall()
        assert len(rows) == 1
        return dict(rows[0])

    def slot_holders(self) -> list[str]:
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            return [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE state != 'stopped'")]


def _absent(path: Path) -> str:
    raise FileNotFoundError(2, "absent", str(path))


@pytest.fixture
def rig() -> Iterator[Rig]:
    with TemporaryDirectory(prefix="latch.", dir=Path.home()) as directory:
        yield Rig(Path(directory))


@pytest.fixture(autouse=True)
def no_process_control(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"latch issued {name}")

        return call

    for target, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (os, "kill"),
        (os, "killpg"),
        (os, "system"),
    ):
        monkeypatch.setattr(target, name, refuse(name))


def test_the_fixture_needs_no_ancestry_mock(rig: Rig) -> None:
    assert night_state._ancestor_stat.__module__ == "agent_board.night_state"
    assert night_state._ancestor_stat(rig.path.parent.parent) == rig.path.parent.parent.lstat()
    rig.journal()
    assert rig.path.lstat().st_mode & 0o777 == 0o600
    assert rig.path.parent.lstat().st_mode & 0o777 == 0o700


def test_only_the_verifier_units_are_queried_and_only_before_open(rig: Rig) -> None:
    run_id = rig.reserve(claim=True)
    outcome = rig.latch(run_id)
    assert outcome.latched == [run_id]
    verifier = [CONFIG.verifier_socket, CONFIG.verifier_unit]
    assert rig.manager.asked == verifier
    assert rig.asked_at_open == [verifier]
    assert CONFIG.guard_unit not in rig.manager.asked


def test_all_open_queries_only_the_verifier_units_while_the_guard_is_active(rig: Rig) -> None:
    run_id = rig.reserve(claim=True)
    outcome = rig.latch(None)
    assert outcome.latched == [run_id]
    assert rig.manager.asked == [CONFIG.verifier_socket, CONFIG.verifier_unit]


def test_the_latch_module_has_no_process_control_surface() -> None:
    names = set(vars(latch))
    assert not names & {"subprocess", "os", "signal", "kill", "killpg"}


def test_named_claimed_run_is_latched_stopping_and_keeps_its_slot(rig: Rig) -> None:
    run_id = rig.reserve(claim=True)
    first = rig.latch(run_id)
    assert (first.latched, first.already, first.no_run, first.refused) == ([run_id], [], False, None)
    row = rig.row(run_id)
    assert (row["state"], row["stop_requested"], row["launch_claimed"], row["reason"]) == (
        "stopping",
        1,
        1,
        None,
    )
    assert rig.slot_holders() == [run_id]
    second = rig.latch(run_id)
    assert (second.latched, second.already, second.no_run, second.refused) == ([], [run_id], False, None)
    assert second.exit_code == first.exit_code == LATCHED_EXIT
    assert rig.row(run_id) == row


def test_named_never_claimed_reservation_is_latched_not_stopped(rig: Rig) -> None:
    run_id = rig.reserve(claim=False)
    assert rig.latch(run_id).latched == [run_id]
    row = rig.row(run_id)
    assert (row["state"], row["stop_requested"], row["launch_claimed"]) == ("stopping", 1, 0)
    assert rig.journal().claim_start(run_id, FENCE) is False
    assert rig.row(run_id) == row
    assert rig.slot_holders() == [run_id]


@pytest.mark.parametrize(
    ("advance", "reason"),
    [
        pytest.param(Clock.expire, "never_claimed_expired", id="deadline"),
        pytest.param(Clock.reboot, "never_claimed_boot_changed", id="boot"),
    ],
)
def test_named_expired_never_claimed_reservation_is_stopped_by_refresh_and_reported_already(
    rig: Rig, advance: Any, reason: str
) -> None:
    run_id = rig.reserve(claim=False)
    advance(rig.clock)
    outcome = rig.latch(run_id)
    assert (outcome.latched, outcome.already, outcome.refused) == ([], [run_id], None)
    row = rig.row(run_id)
    assert (row["state"], row["stop_requested"], row["reason"]) == ("stopped", 1, reason)
    assert rig.slot_holders() == []


def test_all_open_no_run_follows_a_refresh_that_stopped_an_existing_reservation(rig: Rig) -> None:
    run_id = rig.reserve(claim=False)
    rig.clock.expire()
    outcome = rig.latch(None)
    assert (outcome.latched, outcome.already, outcome.no_run, outcome.refused) == ([], [], True, None)
    assert rig.row(run_id)["state"] == "stopped"
    assert rig.row(run_id)["reason"] == "never_claimed_expired"


def test_expired_claimed_run_is_quarantined_and_keeps_its_obligation(rig: Rig) -> None:
    run_id = rig.reserve(claim=True)
    rig.clock.expire()
    outcome = rig.latch(run_id)
    assert (outcome.latched, outcome.already, outcome.refused) == ([], [run_id], None)
    row = rig.row(run_id)
    assert (row["state"], row["stop_requested"], row["launch_claimed"], row["reason"]) == (
        "quarantined",
        1,
        1,
        "deadline_passed",
    )
    assert rig.slot_holders() == [run_id]
    all_open = rig.latch(None)
    assert (all_open.latched, all_open.already, all_open.no_run) == ([], [run_id], False)
    assert rig.row(run_id)["state"] == "quarantined"


def test_reopening_constructor_stops_an_expired_reservation_before_a_wrong_fence_refusal(rig: Rig) -> None:
    run_id = rig.reserve(claim=False)
    before = rig.path.read_bytes()
    rig.clock.expire()
    outcome = rig.latch(run_id, fence=WRONG_FENCE)
    assert outcome.refused == "unknown_run_or_wrong_fence"
    assert outcome.exit_code == REFUSED_EXIT
    assert rig.row(run_id)["state"] == "stopped"
    assert rig.path.read_bytes() != before


def test_wrong_fence_refusal_itself_commits_a_fresh_expiry_refresh(rig: Rig) -> None:
    run_id = rig.reserve(claim=False)
    journal = rig.journal()
    assert rig.row(run_id)["state"] == "reserved"
    rig.clock.expire()
    with pytest.raises(StateError, match="stale_fence"):
        journal.latch_stop(run_id, WRONG_FENCE)
    assert rig.row(run_id)["state"] == "stopped"


def test_unknown_run_refusal_also_commits_the_refresh(rig: Rig) -> None:
    run_id = rig.reserve(claim=True)
    journal = rig.journal()
    rig.clock.expire()
    with pytest.raises(StateError, match="stale_fence"):
        journal.latch_stop(str(uuid4()), FENCE)
    assert rig.row(run_id)["state"] == "quarantined"


def _populated(path: Path) -> str:
    return "populated 1\nfrozen 0\n"


def _unreadable_events(path: Path) -> str:
    raise PermissionError(13, "denied", str(path))


@pytest.mark.parametrize(
    ("shown", "events", "reason"),
    [
        pytest.param(
            "LoadState=loaded\nActiveState=active\nJob=\nControlGroup=\n", None, "ActiveState=active"
        ),
        pytest.param(
            "LoadState=masked\nActiveState=inactive\nJob=\nControlGroup=\n", None, "LoadState=masked"
        ),
        pytest.param("LoadState=loaded\nActiveState=inactive\nJob=7 start\nControlGroup=\n", None, "Job=7"),
        pytest.param(STOPPED, _populated, "cgroup populated or unknown"),
        pytest.param(STOPPED, _unreadable_events, "cgroup.events unreadable"),
        pytest.param(STOPPED + "LoadState=loaded\n", None, "LoadState appears 2 times"),
        pytest.param(None, None, "manager unreadable"),
    ],
    ids=[
        "active",
        "masked",
        "job",
        "cgroup-populated",
        "cgroup-unreadable",
        "duplicate",
        "manager-unreadable",
    ],
)
def test_verifier_refusals_happen_before_the_journal_is_opened(
    rig: Rig, shown: str | None, events: Any, reason: str
) -> None:
    class Failing(ManagerModel):
        def show(self, unit: str, properties: Sequence[str]) -> str:
            super().show(unit, properties)
            if shown is None:
                raise OSError("bus down")
            return shown

    rig.manager = Failing()
    outcome = rig.latch(str(uuid4()), events=events)
    assert outcome.refused is not None and outcome.refused.startswith(f"{CONFIG.verifier_socket}: ")
    assert reason in outcome.refused
    assert outcome.exit_code == REFUSED_EXIT
    assert rig.asked_at_open == []
    assert not rig.path.exists()
    assert not rig.path.parent.exists()


@pytest.mark.parametrize("load_state", ["loaded", "not-found"])
def test_loaded_and_not_found_still_count_as_stopped(rig: Rig, load_state: str) -> None:
    run_id = rig.reserve(claim=True)
    rig.manager.outputs = {
        unit: f"LoadState={load_state}\nActiveState=inactive\nJob=\nControlGroup=\n"
        for unit in (CONFIG.verifier_socket, CONFIG.verifier_unit)
    }
    assert rig.latch(run_id).latched == [run_id]


def test_outcome_json_keys_and_exit_codes_are_unchanged(rig: Rig) -> None:
    assert [field.name for field in dataclasses.fields(Outcome)] == [
        "latched",
        "already",
        "no_run",
        "refused",
    ]
    run_id = rig.reserve(claim=True)
    outcomes = [rig.latch(run_id), rig.latch(run_id), rig.latch(run_id, fence=WRONG_FENCE), rig.latch("x")]
    for outcome in outcomes:
        assert sorted(json.loads(outcome.json())) == OUTCOME_KEYS
    assert [outcome.exit_code for outcome in outcomes] == [0, 0, 1, 1]
    assert json.loads(outcomes[3].json()) == {
        "already": [],
        "latched": [],
        "no_run": False,
        "refused": "invalid_run",
    }
    assert (LATCHED_EXIT, REFUSED_EXIT) == (0, 1)
