import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board.runner import night_containment as containment
from agent_board.runner.night_containment import ContainmentError, RunPlan, unit_argv, verified_empty


@pytest.fixture(autouse=True)
def block_native_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected_native_process")

    monkeypatch.setattr(containment, "_checked", forbidden)
    monkeypatch.setattr("agent_board.runner.night_containment.os.execvpe", forbidden)


def plan(tmp_path: Path, **changes: object) -> RunPlan:
    values: dict[str, Any] = {
        "run_id": str(uuid4()),
        "fence": "a" * 43,
        "principal": "pi@dev",
        "seconds": 1200.0,
        "request_elapsed": 0.25,
        "monotonic_now": 100.0,
        "boot_id": str(uuid4()),
        "uid": os.getuid(),
        "home": tmp_path / "night-home",
        "ordinary_home": tmp_path / "ordinary-home",
        "workdir": tmp_path / "night-worktree",
        "ordinary_workdir": tmp_path / "ordinary-worktree",
        "command": ("happier", "daemon", "start-sync"),
    }
    values.update(changes)
    return RunPlan.create(**values)


def test_unit_backstop_and_startup_guard_share_a_nonextendable_budget(tmp_path: Path) -> None:
    run = plan(tmp_path)
    assert run.deadline_monotonic == 1299.75
    assert run.runtime_limit(100) == 1193.75
    assert run.runtime_limit(200) == 1093.75
    with pytest.raises(ContainmentError, match="expired"):
        run.runtime_limit(1295)
    argv = unit_argv(run, tmp_path / "plan.json", "/usr/bin/python3")
    assert argv[:2] == ("systemd-run", "--user")
    assert "--property=KillMode=control-group" in argv
    assert "--property=SendSIGKILL=yes" in argv
    assert "--property=Restart=no" in argv
    assert "--property=TimeoutStopSec=5" in argv
    assert "--property=RuntimeMaxSec=1193.75" in argv
    assert "--property=MemoryMax=629145600" in argv
    assert argv[-4:] == (
        "agent_board.runner.night_containment",
        "--worker",
        str(tmp_path / "plan.json"),
        run.unit,
    )


@pytest.mark.parametrize("seconds", [math.nan, math.inf, -1, 0, 5, 6, 1201])
def test_invalid_or_too_short_grants_cannot_launch(tmp_path: Path, seconds: float) -> None:
    with pytest.raises(ContainmentError):
        plan(tmp_path, seconds=seconds)


def test_isolation_and_identity_checks(tmp_path: Path) -> None:
    for changes in (
        {"run_id": "../ordinary"},
        {"fence": ""},
        {"principal": "pi@ops"},
        {"principal": "pi@heimdall"},
        {"request_elapsed": -1},
        {"request_elapsed": 1200},
        {"home": tmp_path / "ordinary-home"},
        {"workdir": tmp_path / "ordinary-worktree"},
        {"command": ()},
    ):
        with pytest.raises(ContainmentError):
            plan(tmp_path, **changes)
    (tmp_path / "alias").symlink_to(tmp_path / "ordinary-home")
    with pytest.raises(ContainmentError):
        plan(tmp_path, home=tmp_path / "alias")


def test_absence_alone_or_an_unrelated_unit_is_not_stop_proof(tmp_path: Path) -> None:
    run = plan(tmp_path)
    properties = {
        "Id": run.unit,
        "Description": f"Switchboard autonomy {run.marker}",
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "ControlGroup": "",
        "InvocationID": "b" * 32,
    }
    root = tmp_path / "cgroup"
    assert not verified_empty(run, properties, run.boot_id, "b" * 32, root)
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpu memory pids")
    assert verified_empty(run, properties, run.boot_id, "b" * 32, root)
    for changed in (
        {"Id": "ordinary.service"},
        {"ActiveState": "active"},
        {"LoadState": "not-found"},
        {"InvocationID": "c" * 32},
        {"ControlGroup": "/unrelated"},
    ):
        assert not verified_empty(run, properties | changed, run.boot_id, "b" * 32, tmp_path / "cgroup")
    assert not verified_empty(run, properties, str(uuid4()), "b" * 32, tmp_path / "cgroup")
    assert not verified_empty(run, properties, run.boot_id, "", tmp_path / "cgroup")
    unloaded = properties | {"LoadState": "not-found", "InvocationID": ""}
    assert verified_empty(run, unloaded, run.boot_id, "b" * 32, root)
    assert not verified_empty(run, unloaded, run.boot_id, "", root)
    assert not verified_empty(run, properties | {"Description": "ordinary"}, run.boot_id, "b" * 32, root)


def test_journal_is_durable_exclusive_and_revalidated(tmp_path: Path) -> None:
    run = plan(tmp_path)
    journal = tmp_path / "plan.json"
    run.save(journal)
    assert RunPlan.load(journal) == run
    assert journal.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        run.save(journal)
    record = json.loads(journal.read_text())
    record["plan"]["deadline_monotonic"] = 5000
    journal.write_text(json.dumps(record))
    with pytest.raises(ContainmentError, match="invalid_plan"):
        RunPlan.load(journal)


def test_populated_or_unreadable_cgroup_is_not_stop_proof(tmp_path: Path) -> None:
    run = plan(tmp_path)
    root = tmp_path / "cgroup"
    group = root / run.cgroup.lstrip("/")
    group.mkdir(parents=True)
    (root / "cgroup.controllers").write_text("cpu memory pids")
    properties = {
        "Id": run.unit,
        "Description": f"Switchboard autonomy {run.marker}",
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "ControlGroup": run.cgroup,
        "InvocationID": "b" * 32,
    }
    assert not verified_empty(run, properties, run.boot_id, "b" * 32, root)
    (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    assert not verified_empty(run, properties, run.boot_id, "b" * 32, root)
    (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    assert verified_empty(run, properties, run.boot_id, "b" * 32, root)


def test_worker_rechecks_deadline_and_arms_absolute_stop_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = plan(tmp_path)
    journal = tmp_path / "plan.json"
    run.save(journal)
    monkeypatch.setattr(containment, "_kernel_identity", lambda: (run.boot_id, f"0::{run.cgroup}"))
    monkeypatch.setattr("agent_board.runner.night_containment.time.monotonic", lambda: 102.0)
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    calls: list[tuple[str, ...]] = []

    def checked(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="101000000\n", stderr="")

    executed: list[tuple[str, tuple[str, ...], dict[str, str]]] = []

    def execute(file: str, argv: tuple[str, ...], env: dict[str, str]) -> None:
        executed.append((file, argv, env))

    monkeypatch.setattr(containment, "_checked", checked)
    monkeypatch.setattr("agent_board.runner.night_containment.os.execvpe", execute)
    containment.worker(journal, run.unit)
    assert len(calls) == 1 and "--on-boot=1293.75s" in calls[0]
    assert "--timer-property=WakeSystem=no" in calls[0]
    assert calls[0][-4:] == ("/usr/bin/systemctl", "--user", "stop", run.unit)
    assert executed[0][1] == run.command
    assert executed[0][2]["HAPPIER_HOME_DIR"] == str(run.home)
    observation = json.loads(journal.with_suffix(".observed.json").read_text())
    assert observation["invocation"] == "b" * 32 and observation["marker"] == run.marker
    with pytest.raises(FileExistsError):
        containment.worker(journal, run.unit)
    assert len(executed) == 1


def test_deadline_timer_failure_never_executes_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = plan(tmp_path)
    journal = tmp_path / "plan.json"
    run.save(journal)
    monkeypatch.setattr(containment, "_kernel_identity", lambda: (run.boot_id, f"0::{run.cgroup}"))
    monkeypatch.setattr("agent_board.runner.night_containment.time.monotonic", lambda: 102.0)
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)

    def failed(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        raise ContainmentError("timer_failed")

    monkeypatch.setattr(containment, "_checked", failed)
    with pytest.raises(ContainmentError, match="timer_failed"):
        containment.worker(journal, run.unit)
    assert journal.with_suffix(".observed.json").exists()
    with pytest.raises(FileExistsError):
        containment.worker(journal, run.unit)


@pytest.mark.parametrize("failure", ["expired", "boot", "cgroup", "invocation"])
def test_worker_refuses_expired_or_uncontained_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    run = plan(tmp_path)
    journal = tmp_path / "plan.json"
    run.save(journal)
    boot = str(uuid4()) if failure == "boot" else run.boot_id
    group = "0::/ordinary" if failure == "cgroup" else f"0::{run.cgroup}"
    monkeypatch.setattr(containment, "_kernel_identity", lambda: (boot, group))
    monkeypatch.setattr(
        "agent_board.runner.night_containment.time.monotonic",
        lambda: 1299.0 if failure == "expired" else 102.0,
    )
    monkeypatch.setenv("INVOCATION_ID", "" if failure == "invocation" else "b" * 32)
    with pytest.raises(ContainmentError):
        containment.worker(journal, run.unit)
    assert not journal.with_suffix(".observed.json").exists()
