import errno
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night import __main__ as entry
from agent_board.night.config import VerifierConfigError, parse
from agent_board.night.latch import latch_stop, unit_not_stopped
from agent_board.night_state import ClockReading, Grant, LocalJournal

BOOT = str(uuid4())
UID = 1001
FENCE = "f" * 43
STOPPED = "LoadState=loaded\nActiveState=inactive\nJob=\nControlGroup=\n"


def _table(**changes: Any) -> dict[str, Any]:
    table: dict[str, Any] = {
        "box": "huginn",
        "principal": "verifier@huginn",
        "namespace": "switchboard",
        "board_url": "https://board.test.invalid",
        "signer_key": "/etc/switchboard-night/verifier_ed25519",
        "state_path": "/var/lib/switchboard-night/journal.sqlite3",
        "slice": "switchboard-night.slice",
        "launcher": {
            "python": "/opt/switchboard-night/releases/abc/venv/bin/python",
            "module": "agent_board.night.launch",
        },
        "agents": {
            "pi@huginn": {
                "uid": UID,
                "home": "/home/agent",
                "happier_home": "/srv/h",
                "workdir_root": "/srv/w",
            }
        },
        "limits": {"memory_max": "600M", "tasks_max": 128, "cpu_quota": "100%", "stop_grace_seconds": 5},
    }
    table.update(changes)
    return table


class Manager:
    def __init__(self) -> None:
        self.outputs: dict[str, str] = {}
        self.failure: OSError | None = None

    def show(self, unit: str, properties: Sequence[str]) -> str:
        if self.failure is not None:
            raise self.failure
        return self.outputs.get(unit, STOPPED)


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


def _absent(path: Path) -> str:
    raise FileNotFoundError(errno.ENOENT, "absent", str(path))


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.config = parse(_table())
        self.journal = LocalJournal(
            tmp_path / "state" / "journal.sqlite3",
            box="huginn",
            principals={"pi@huginn": UID},
            clock=lambda: ClockReading(BOOT, 100.0, 1_000.0),
        )
        self.manager = Manager()

    def open_run(self, claim: bool = True) -> str:
        run_id = str(uuid4())
        self.journal.reserve(Grant(run_id, FENCE, "pi@huginn", UID, BOOT, 700.0, 1_600.0))
        if claim:
            self.journal.claim_start(run_id, FENCE)
        return run_id

    def latch(self, run_id: str | None, fence: str | None = FENCE, events: Any = _absent) -> Any:
        return latch_stop(self.config, self.journal, self.manager, run_id=run_id, fence=fence, events=events)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.mark.parametrize(
    ("shown", "reason"),
    [
        pytest.param(
            "LoadState=loaded\nActiveState=active\nJob=\nControlGroup=\n", "ActiveState=active", id="active"
        ),
        pytest.param(
            "LoadState=loaded\nActiveState=deactivating\nJob=\nControlGroup=\n",
            "deactivating",
            id="deactivating",
        ),
        pytest.param(
            "LoadState=loaded\nActiveState=inactive\nControlGroup=\n", "Job appears 0 times", id="no-job-line"
        ),
        pytest.param(
            "LoadState=loaded\nActiveState=inactive\nJob=12 stop\nControlGroup=\n", "Job=12", id="pending-job"
        ),
        pytest.param(STOPPED + "Job=\n", "Job appears 2 times", id="repeated-job"),
        pytest.param(
            "LoadState=masked\nActiveState=inactive\nJob=\nControlGroup=\n", "LoadState=masked", id="masked"
        ),
        pytest.param("", "LoadState appears 0 times", id="empty-output"),
        pytest.param(
            " LoadState=loaded\nActiveState=inactive\nJob=\nControlGroup=\n",
            "LoadState appears 0",
            id="leading-space",
        ),
    ],
)
def test_the_daemon_proof_needs_every_property_exactly_once_and_stopped(shown: str, reason: str) -> None:
    manager = Manager()
    manager.outputs["x.service"] = shown

    assert reason in (unit_not_stopped(manager, "x.service", _absent) or "proven"), shown


def test_a_populated_or_unreadable_cgroup_is_not_proof() -> None:
    manager = Manager()

    def populated(path: Path) -> str:
        return "populated 1\nfrozen 0\n"

    def unreadable(path: Path) -> str:
        raise PermissionError(errno.EACCES, "denied", str(path))

    assert "populated" in (unit_not_stopped(manager, "x.service", populated) or "")
    assert "unreadable" in (unit_not_stopped(manager, "x.service", unreadable) or "")
    assert unit_not_stopped(manager, "x.service", lambda path: "populated 0\nfrozen 0\n") is None
    assert unit_not_stopped(manager, "x.service", _absent) is None


def test_a_live_verifier_blocks_any_latch_and_changes_nothing(rig: Rig) -> None:
    run_id = rig.open_run()
    rig.manager.outputs[rig.config.verifier_unit] = (
        "LoadState=loaded\nActiveState=active\nJob=\nControlGroup=/x\n"
    )

    outcome = rig.latch(run_id)

    assert outcome.refused is not None and outcome.exit_code == 1, outcome
    assert rig.journal.active()[0].stop_requested is False, (
        "a latch was written while the request writer lived"
    )


def test_an_unreadable_manager_refuses(rig: Rig) -> None:
    rig.manager.failure = OSError("bus down")

    assert rig.latch(None).refused is not None


def test_a_named_run_is_latched_once_then_reported_already(rig: Rig) -> None:
    run_id = rig.open_run()

    first = rig.latch(run_id)
    second = rig.latch(run_id)

    assert (first.latched, first.exit_code) == ([run_id], 0), first
    assert second.already == [run_id] and second.latched == [], second
    assert rig.journal.active()[0].state == "stopping"


def test_a_wrong_fence_or_unknown_run_is_refused_not_reported_as_latched(rig: Rig) -> None:
    run_id = rig.open_run()

    wrong = rig.latch(run_id, fence="g" * 43)
    unknown = rig.latch(str(uuid4()))
    malformed = rig.latch("not-a-uuid")

    assert wrong.refused == unknown.refused == "unknown_run_or_wrong_fence", (wrong, unknown)
    assert malformed.refused == "invalid_run"
    assert not rig.journal.active()[0].stop_requested


def test_all_open_latches_every_open_run_without_fences(rig: Rig) -> None:
    run_id = rig.open_run()

    outcome = rig.latch(None, fence=None)

    assert outcome.latched == [run_id] and not outcome.no_run and outcome.exit_code == 0, outcome
    assert json.loads(outcome.json()) == {
        "latched": [run_id],
        "already": [],
        "no_run": False,
        "refused": None,
    }


def test_all_open_with_no_open_runs_reports_no_run_distinctly(rig: Rig) -> None:
    outcome = rig.latch(None, fence=None)

    assert outcome.no_run and outcome.latched == [] and outcome.exit_code == 0, outcome


def test_a_smoke_verifier_name_is_only_accepted_with_the_acceptance_stub() -> None:
    stub = {
        "python": "/opt/switchboard-night/releases/abc/venv/bin/python",
        "module": "agent_board.night.acceptance_stub",
    }

    smoke = parse(
        _table(
            verifier_unit="switchboard-night-verifier-smoke.service",
            verifier_socket="switchboard-night-verifier-smoke.socket",
            launcher=stub,
        )
    )

    assert (smoke.verifier_unit, smoke.verifier_socket) == (
        "switchboard-night-verifier-smoke.service",
        "switchboard-night-verifier-smoke.socket",
    )
    with pytest.raises(VerifierConfigError, match="acceptance stub"):
        parse(_table(verifier_unit="switchboard-night-verifier-smoke.service"))
    with pytest.raises(VerifierConfigError, match="verifier_socket"):
        parse(_table(verifier_socket="ssh.socket", launcher=stub))


def test_latch_stop_needs_exactly_one_of_a_run_id_or_all_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(entry, "load", lambda path: parse(_table()))
    monkeypatch.setattr(entry, "LocalJournal", lambda *args, **kwargs: None)
    monkeypatch.setattr(entry, "Ledger", lambda path: None)

    for argv in (
        ["latch-stop", "--config", "/etc/x.toml"],
        ["latch-stop", "--config", "/etc/x.toml", str(uuid4()), "--all-open"],
    ):
        with pytest.raises(SystemExit) as raised:
            entry.main(argv)
        assert raised.value.code == 2
