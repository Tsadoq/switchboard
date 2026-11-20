import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board.night.assignment import Assignment
from agent_board.night.cli_launch import (
    EXIT_CLI_FAILED,
    CliSettings,
    LiveCliHost,
    cli_argv,
    cli_settings_from,
    launch,
)
from agent_board.night.config import VerifierConfigError, parse
from agent_board.night.controller import RunSpec, unit_name
from agent_board.night.launch import EXIT_BUDGET, EXIT_DONE, LaunchRefused
from agent_board.night.systemd import run_argv

UID = os.getuid()
RUN = str(uuid4())


def _table(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "box": "dev",
        "principal": "verifier@dev",
        "namespace": "switchboard",
        "board_url": "https://board.test.invalid",
        "signer_key": "/etc/switchboard-night/verifier_ed25519",
        "state_path": "/var/lib/switchboard-night/journal.sqlite3",
        "slice": "switchboard-night.slice",
        "launcher": {"python": "/opt/switchboard-night/bin/python", "module": "agent_board.night.launch"},
        "agents": {"pi@dev": agent},
        "limits": {"memory_max": "600M", "tasks_max": 128, "cpu_quota": "100%", "stop_grace_seconds": 5},
    }


EXISTING = {
    "uid": 1000,
    "login": "existing",
    "cli": "/home/dev/.local/bin/pi",
    "login_paths": ["/home/dev/.pi"],
    "home": "/home/dev",
    "workdir_root": "/srv/night/pi",
}


def test_an_existing_login_agent_needs_no_night_uid_or_happier_home() -> None:
    agent = parse(_table(dict(EXISTING))).agents["pi@dev"]

    assert agent.existing_login and agent.exec_uid == 1000
    assert agent.cli == Path("/home/dev/.local/bin/pi")
    assert agent.login_paths == (Path("/home/dev/.pi"),)
    assert agent.happier_home is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param({"login": "shared"}, "login must be", id="unknown-login"),
        pytest.param({"cli": None}, "needs cli", id="no-cli"),
        pytest.param({"login_paths": []}, "non-empty", id="no-login-paths"),
        pytest.param({"login_paths": ["/etc/shadow"]}, "inside home", id="path-outside-home"),
        pytest.param({"login_paths": ["relative"]}, "normalized absolute", id="relative-path"),
        pytest.param({"night_uid": 3001, "night_gid": 3001}, "without night_uid", id="with-night-uid"),
        pytest.param({"happier_home": "/var/lib/h"}, "without night_uid", id="with-happier-home"),
    ],
)
def test_bad_existing_login_agents_are_config_errors(change: dict[str, Any], message: str) -> None:
    agent = {**EXISTING, **change}
    agent = {key: value for key, value in agent.items() if value is not None}

    with pytest.raises(VerifierConfigError, match=message):
        parse(_table(agent))


def test_an_isolated_agent_cannot_name_a_cli_and_still_needs_its_happier_home() -> None:
    isolated = {key: value for key, value in EXISTING.items() if key != "login"}
    with pytest.raises(VerifierConfigError, match="need login"):
        parse(_table(isolated))
    bare = {"uid": 1000, "night_uid": 3001, "night_gid": 3001, "home": "/home/dev", "workdir_root": "/srv/n"}
    with pytest.raises(VerifierConfigError, match="happier_home"):
        parse(_table(bare))


def test_an_existing_login_unit_may_write_only_the_workdir_login_paths_and_state() -> None:
    config = parse(_table(dict(EXISTING)))
    spec = RunSpec(RUN, unit_name(RUN), config.agents["pi@dev"], 600.0)

    argv = run_argv(config, Path("/etc/switchboard-night/verifier.toml"), spec, Path("/opt/py"))

    assert "--property=ReadWritePaths=/srv/night/pi /home/dev/.pi /var/lib/switchboard-night" in argv
    assert "--property=MemoryMax=600M" in argv and "--property=RuntimeMaxSec=600" in argv


def _environ(tmp_path: Path, **changes: str) -> dict[str, str]:
    environ = {
        "SWITCHBOARD_NIGHT_LOGIN": "existing",
        "SWITCHBOARD_NIGHT_PRINCIPAL": "pi@dev",
        "HOME": str(tmp_path),
        "SWITCHBOARD_NIGHT_WORKDIR_ROOT": str(tmp_path / "work"),
        "SWITCHBOARD_NIGHT_CLI": str(tmp_path / "bin" / "pi"),
        "SWITCHBOARD_NIGHT_DEADLINE_UTC": "2000.0",
        "SWITCHBOARD_NIGHT_UID": str(UID),
        "SWITCHBOARD_NIGHT_DAY_UID": str(UID),
    }
    environ.update(changes)
    return environ


def test_settings_come_only_from_an_existing_login_environment(tmp_path: Path) -> None:
    settings = cli_settings_from(RUN, _environ(tmp_path), UID)

    assert settings.workdir == tmp_path / "work" / RUN
    assert settings.cli == tmp_path / "bin" / "pi"


@pytest.mark.parametrize(
    "changes",
    [
        {"SWITCHBOARD_NIGHT_LOGIN": "isolated"},
        {"SWITCHBOARD_NIGHT_CLI": "pi"},
        {"SWITCHBOARD_NIGHT_DEADLINE_UTC": "nan"},
        {"SWITCHBOARD_NIGHT_UID": "1"},
        {"SWITCHBOARD_NIGHT_DAY_UID": "1"},
    ],
)
def test_bad_environments_refuse(tmp_path: Path, changes: dict[str, str]) -> None:
    with pytest.raises(LaunchRefused):
        cli_settings_from(RUN, _environ(tmp_path, **changes), UID)


def _assignment(**changes: Any) -> Assignment:
    values: dict[str, Any] = {
        "run_id": RUN,
        "principal": "pi@dev",
        "repository_key": "switchboard",
        "base_commit": "a" * 40,
        "prompt": "work the held wakes",
        "backend": "pi",
        "model": None,
        "permission_mode": "auto",
        "turn_seconds": 600,
        "deadline_utc": 2_000.0,
    }
    values.update(changes)
    return Assignment(**values)


def _settings(tmp_path: Path, cli: str = "pi") -> CliSettings:
    return CliSettings(RUN, "pi@dev", UID, tmp_path, tmp_path / "work", tmp_path / "bin" / cli, 2_000.0)


def test_cli_argv_per_backend(tmp_path: Path) -> None:
    assert cli_argv(_settings(tmp_path), _assignment()) == [
        str(tmp_path / "bin" / "pi"),
        "-p",
        "work the held wakes",
    ]
    claude = _settings(tmp_path, "claude")
    assert cli_argv(claude, _assignment(backend="claude", permission_mode="default", model="opus"))[3:] == [
        "--model",
        "opus",
        "--permission-mode",
        "dontAsk",
    ]
    assert cli_argv(claude, _assignment(backend="claude", permission_mode="read_only"))[-1] == "plan"


@pytest.mark.parametrize(
    ("assignment", "reason"),
    [
        pytest.param(_assignment(backend="claude"), "backend_does_not_match_cli", id="wrong-cli"),
        pytest.param(_assignment(permission_mode="default"), "permission_mode_unsupported", id="pi-not-auto"),
    ],
)
def test_cli_argv_refuses_mismatches(tmp_path: Path, assignment: Assignment, reason: str) -> None:
    with pytest.raises(LaunchRefused, match=reason):
        cli_argv(_settings(tmp_path), assignment)


class Host:
    def __init__(self, code: int | None = 0) -> None:
        self.code = code
        self.cloned: list[tuple[Path, Path, str]] = []
        self.ran: list[tuple[list[str], Path, float]] = []
        self.trusted = True

    def uid(self) -> int:
        return UID

    def containers_trusted(self, path: Path) -> bool:
        return self.trusted

    def clone(self, source: Path, destination: Path, commit: str) -> None:
        self.cloned.append((source, destination, commit))

    def run_cli(self, argv: Sequence[str], cwd: Path, timeout: float) -> int | None:
        self.ran.append((list(argv), cwd, timeout))
        return self.code

    def utc(self) -> float:
        return 1_700.0


class Assignments:
    def __init__(self, assignment: Assignment) -> None:
        self.assignment = assignment

    def fetch(self, run_id: str, principal: str) -> Assignment:
        return self.assignment


class Repositories:
    def resolve(self, repository_key: str, principal: str) -> Path:
        return Path("/var/lib/switchboard-night/mirrors") / f"{repository_key}.git"


def _prepared(tmp_path: Path) -> CliSettings:
    settings = _settings(tmp_path)
    settings.workdir_root.mkdir(mode=0o700)
    settings.cli.parent.mkdir()
    settings.cli.write_text("#!/bin/sh\n")
    settings.cli.chmod(0o700)
    return settings


@pytest.mark.parametrize(("code", "expected"), [(0, EXIT_DONE), (3, EXIT_CLI_FAILED), (None, EXIT_BUDGET)])
def test_launch_clones_then_runs_the_cli_in_the_run_workdir_within_budget(
    tmp_path: Path, code: int | None, expected: int
) -> None:
    settings, host = _prepared(tmp_path), Host(code)

    result = launch(settings, host=host, assignments=Assignments(_assignment()), repositories=Repositories())

    assert result == expected
    assert host.cloned == [
        (Path("/var/lib/switchboard-night/mirrors/switchboard.git"), settings.workdir, "a" * 40)
    ]
    assert host.ran == [([str(settings.cli), "-p", "work the held wakes"], settings.workdir, 300.0)]


def test_launch_refuses_before_cloning(tmp_path: Path) -> None:
    settings, host = _prepared(tmp_path), Host()
    settings.workdir.mkdir()

    with pytest.raises(LaunchRefused, match="workdir_exists"):
        launch(settings, host=host, assignments=Assignments(_assignment()), repositories=Repositories())
    settings.workdir.rmdir()
    settings.cli.chmod(0o600)
    with pytest.raises(LaunchRefused, match="cli_not_executable"):
        launch(settings, host=host, assignments=Assignments(_assignment()), repositories=Repositories())
    settings.cli.chmod(0o700)
    with pytest.raises(LaunchRefused, match="assignment_mismatch"):
        launch(
            settings,
            host=host,
            assignments=Assignments(_assignment(run_id=str(uuid4()))),
            repositories=Repositories(),
        )
    assert host.cloned == [] and host.ran == []


def test_the_live_host_kills_a_cli_that_outlives_its_budget(tmp_path: Path) -> None:
    script = tmp_path / "slow"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    script.chmod(stat.S_IRWXU)
    host = LiveCliHost(_settings(tmp_path))

    assert host.run_cli([str(script)], tmp_path, 0.5) is None
    assert host.run_cli(["/bin/sh", "-c", "exit 4"], tmp_path, 5) == 4


def _identity_config(tmp_path: Path) -> Path:
    conf = tmp_path / "agent-identity.conf"
    conf.write_text(
        '{"kinds": [{"ai_agent": "claude-code", "name": "claude"}, {"ai_agent": "pi", "name": "pi"}]}'
    )
    return conf


def test_the_live_cli_environment_carries_the_board_identity_and_tool_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    claude = replace(settings, principal="claude@dev")

    pi_env = LiveCliHost(settings, _identity_config(tmp_path))._env
    claude_env = LiveCliHost(claude, _identity_config(tmp_path))._env

    assert pi_env["AI_AGENT"] == "pi" and claude_env["AI_AGENT"] == "claude-code"
    assert pi_env["PATH"].split(":") == [
        str(tmp_path / "bin"),
        str(tmp_path / ".local" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


def test_a_kind_missing_from_the_identity_config_refuses(tmp_path: Path) -> None:
    other = replace(_settings(tmp_path), principal="codex@dev")

    with pytest.raises(LaunchRefused, match="identity_kind_not_configured"):
        LiveCliHost(other, _identity_config(tmp_path))
    with pytest.raises(LaunchRefused, match="identity_config_unreadable"):
        LiveCliHost(_settings(tmp_path), tmp_path / "missing.conf")


class SlowClone(Host):
    def __init__(self, setup_seconds: float) -> None:
        super().__init__()
        self.now = 1_000.0
        self.setup = setup_seconds

    def clone(self, source: Path, destination: Path, commit: str) -> None:
        super().clone(source, destination, commit)
        self.now += self.setup

    def utc(self) -> float:
        return self.now


def test_setup_time_is_charged_and_a_clone_past_the_deadline_never_starts_the_cli(tmp_path: Path) -> None:
    settings = _prepared(tmp_path)

    host = SlowClone(100.0)
    assert (
        launch(settings, host=host, assignments=Assignments(_assignment()), repositories=Repositories()) == 0
    )
    assert host.ran[0][2] == 500.0

    late = SlowClone(1_500.0)
    with pytest.raises(LaunchRefused, match="run_deadline_passed_during_setup") as refused:
        launch(settings, host=late, assignments=Assignments(_assignment()), repositories=Repositories())
    assert refused.value.exit_code == EXIT_BUDGET
    assert late.ran == []
