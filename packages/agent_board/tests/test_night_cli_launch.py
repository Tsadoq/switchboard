import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
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
    check_auth,
    cli_argv,
    cli_settings_from,
    existing_token,
    launch,
)
from agent_board.night.config import VerifierConfigError, parse
from agent_board.night.controller import RunSpec, unit_name
from agent_board.night.environment import MIRROR_ROOT
from agent_board.night.launch import EXIT_BUDGET, EXIT_DONE, EXIT_PREFLIGHT, LaunchRefused
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

    def run_cli(
        self, argv: Sequence[str], cwd: Path, timeout: float, extra_env: Mapping[str, str]
    ) -> int | None:
        self.ran.append((list(argv), cwd, timeout))
        self.extra_env = dict(extra_env)
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
        return Path("/var/lib/switchboard-night-mirrors") / f"{repository_key}.git"


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
        (Path("/var/lib/switchboard-night-mirrors/switchboard.git"), settings.workdir, "a" * 40)
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
    host = LiveCliHost(_settings(tmp_path), _identity_config(tmp_path))

    assert host.run_cli([str(script)], tmp_path, 0.5, {}) is None
    assert host.run_cli(["/bin/sh", "-c", "exit 4"], tmp_path, 5, {}) == 4


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


def test_the_mirror_root_lies_outside_the_private_state_directory() -> None:
    config = parse(_table(dict(EXISTING)))

    assert not Path(MIRROR_ROOT).is_relative_to(config.state_path.parent)


@pytest.mark.parametrize(
    "state_path", ["/var/lib/switchboard-night-mirrors/state/journal.sqlite3", "/var/lib/journal.sqlite3"]
)
def test_a_state_directory_overlapping_the_mirror_root_is_refused(state_path: str) -> None:
    with pytest.raises(VerifierConfigError, match="overlap the mirror root"):
        parse({**_table(dict(EXISTING)), "state_path": state_path})


def _toy_mirror(root: Path) -> tuple[Path, str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    mirror, source = root / "mirror.git", root / "src"
    subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True, env=env)
    subprocess.run(["git", "init", "-q", str(source)], check=True, env=env)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=a",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(source), "push", "-q", str(mirror), "HEAD:refs/heads/main"], check=True, env=env
    )
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True
    )
    return mirror, head.stdout.strip()


def test_a_foreign_owned_mirror_clones_with_trust_for_exactly_that_mirror(tmp_path: Path) -> None:
    mirror, commit = _toy_mirror(tmp_path)
    settings = _prepared(tmp_path)
    host = LiveCliHost(settings, _identity_config(tmp_path))
    host._env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"

    host.clone(mirror, settings.workdir, commit)

    assert (settings.workdir / ".git").is_dir()
    assert list(settings.workdir_root.iterdir()) == [settings.workdir], (
        "the per-clone trust file was left behind"
    )


def test_without_the_per_clone_trust_git_refuses_the_foreign_mirror(tmp_path: Path) -> None:
    mirror, _ = _toy_mirror(tmp_path)
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
    }

    refused = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", "--", str(mirror), str(tmp_path / "plain")],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )

    assert refused.returncode != 0 and "dubious ownership" in refused.stderr


CLAUDE_EXISTING = {
    **EXISTING,
    "cli": "/home/dev/.local/bin/claude",
    "login_paths": ["/home/dev/.claude", "/home/dev/.claude.json"],
    "token_file": "/home/dev/.claude/oauth-token.env",
}
TOKEN = "sk-ant-oat01-" + "A" * 40


def test_a_claude_existing_login_may_name_its_own_token_file() -> None:
    agent = parse(_table(dict(CLAUDE_EXISTING))).agents["pi@dev"]

    assert agent.token_file == Path("/home/dev/.claude/oauth-token.env")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            {"cli": "/home/dev/.local/bin/pi", "login_paths": ["/home/dev/.pi"]}, "only supported", id="pi"
        ),
        pytest.param({"token_file": "/etc/oauth-token.env"}, "inside home", id="outside-home"),
        pytest.param({"token_file": "token.env"}, "normalized absolute", id="relative"),
    ],
)
def test_bad_token_files_are_config_errors(change: dict[str, Any], message: str) -> None:
    with pytest.raises(VerifierConfigError, match=message):
        parse(_table({**CLAUDE_EXISTING, **change}))


def test_an_isolated_agent_cannot_name_a_token_file() -> None:
    isolated = {
        "uid": 1000,
        "night_uid": 3001,
        "night_gid": 3001,
        "home": "/home/dev",
        "happier_home": "/var/lib/h",
        "workdir_root": "/srv/n",
        "token_file": "/home/dev/.claude/oauth-token.env",
    }
    with pytest.raises(VerifierConfigError, match="need login"):
        parse(_table(isolated))


def _token_settings(tmp_path: Path, text: str | bytes | None, mode: int = 0o600) -> CliSettings:
    token_file = tmp_path / "oauth-token.env"
    if text is not None:
        token_file.write_bytes(text.encode() if isinstance(text, str) else text)
        token_file.chmod(mode)
    return replace(_settings(tmp_path, "claude"), token_file=token_file)


CLAUDE_ASSIGNMENT = _assignment(backend="claude", permission_mode="auto")


@pytest.mark.parametrize(
    "text",
    [
        f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n",
        f'# day login\n\nOTHER=ignored\nCLAUDE_CODE_OAUTH_TOKEN="{TOKEN}"\n',
        f"CLAUDE_CODE_OAUTH_TOKEN='{TOKEN}'",
    ],
)
def test_the_existing_token_is_read_as_that_one_variable_alone(tmp_path: Path, text: str) -> None:
    settings = _token_settings(tmp_path, text)

    assert existing_token(settings, CLAUDE_ASSIGNMENT, UID) == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}


@pytest.mark.parametrize(
    ("text", "mode", "reason"),
    [
        pytest.param(None, 0o600, "existing_token_unavailable", id="missing"),
        pytest.param(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}", 0o620, "not_the_accounts", id="group-writable"),
        pytest.param(
            f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}", 0o000, "existing_token_unavailable", id="unreadable"
        ),
        pytest.param("OTHER=1\n", 0o600, "malformed", id="no-token"),
        pytest.param(
            f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\nCLAUDE_CODE_OAUTH_TOKEN={TOKEN}",
            0o600,
            "malformed",
            id="twice",
        ),
        pytest.param("CLAUDE_CODE_OAUTH_TOKEN=\n", 0o600, "malformed", id="empty"),
        pytest.param("export CLAUDE_CODE_OAUTH_TOKEN=x\n", 0o600, "malformed", id="shell-syntax"),
        pytest.param("CLAUDE_CODE_OAUTH_TOKEN=$(id)\n", 0o600, "malformed", id="substitution"),
        pytest.param("garbage\n", 0o600, "malformed", id="no-equals"),
        pytest.param(b"CLAUDE_CODE_OAUTH_TOKEN=\xff\xfe\n", 0o600, "malformed", id="non-ascii"),
        pytest.param("X=" + "y" * 5000, 0o600, "malformed", id="oversized"),
    ],
)
def test_bad_token_files_refuse(tmp_path: Path, text: str | bytes | None, mode: int, reason: str) -> None:
    if mode == 0 and os.geteuid() == 0:
        pytest.skip("root reads unreadable files")
    settings = _token_settings(tmp_path, text, mode)

    with pytest.raises(LaunchRefused, match=reason):
        existing_token(settings, CLAUDE_ASSIGNMENT, UID)


def test_a_token_file_of_another_account_or_a_symlink_refuses(tmp_path: Path) -> None:
    settings = _token_settings(tmp_path, f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}")
    with pytest.raises(LaunchRefused, match="not_the_accounts"):
        existing_token(settings, CLAUDE_ASSIGNMENT, UID + 1)

    link = tmp_path / "link.env"
    link.symlink_to(settings.token_file or tmp_path)
    with pytest.raises(LaunchRefused, match="existing_token_unavailable"):
        existing_token(replace(settings, token_file=link), CLAUDE_ASSIGNMENT, UID)


def test_a_token_file_refuses_for_a_backend_other_than_claude(tmp_path: Path) -> None:
    settings = _token_settings(tmp_path, f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}")

    with pytest.raises(LaunchRefused, match="token_file_unsupported_backend"):
        existing_token(settings, _assignment(), UID)


def test_launch_passes_the_token_only_to_the_cli_and_refuses_before_cloning_without_it(
    tmp_path: Path,
) -> None:
    settings = replace(_prepared(tmp_path), cli=tmp_path / "bin" / "claude")
    settings.cli.write_text("#!/bin/sh\n")
    settings.cli.chmod(0o700)
    good = tmp_path / "token.env"
    good.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    good.chmod(0o600)
    host = Host()

    launch(
        replace(settings, token_file=good),
        host=host,
        assignments=Assignments(CLAUDE_ASSIGNMENT),
        repositories=Repositories(),
    )
    assert host.extra_env == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    assert TOKEN not in " ".join(host.ran[0][0])

    refusing = Host()
    with pytest.raises(LaunchRefused, match="existing_token_unavailable"):
        launch(
            replace(settings, token_file=tmp_path / "absent.env"),
            host=refusing,
            assignments=Assignments(CLAUDE_ASSIGNMENT),
            repositories=Repositories(),
        )
    assert refusing.cloned == [] and refusing.ran == []


def test_the_live_host_gives_the_token_to_the_child_and_nothing_else_from_the_file(tmp_path: Path) -> None:
    host = LiveCliHost(_settings(tmp_path), _identity_config(tmp_path))
    out = tmp_path / "env.txt"
    probe = f"import os; open({str(out)!r}, 'w').write(repr(sorted(os.environ)))"

    assert host.run_cli([sys.executable, "-c", probe], tmp_path, 10, {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}) == 0
    names = out.read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN" in names and "OTHER" not in names


def test_settings_carry_the_token_file_path_from_the_environment(tmp_path: Path) -> None:
    environ = _environ(tmp_path, SWITCHBOARD_NIGHT_TOKEN_FILE=str(tmp_path / "t.env"))

    assert cli_settings_from(RUN, environ, UID).token_file == tmp_path / "t.env"
    with pytest.raises(LaunchRefused, match="unsafe_path"):
        cli_settings_from(RUN, _environ(tmp_path, SWITCHBOARD_NIGHT_TOKEN_FILE="relative"), UID)


def _fake_cli(tmp_path: Path, name: str) -> tuple[Path, Path]:
    record = tmp_path / f"{name}.record"
    cli = tmp_path / "bin" / name
    cli.parent.mkdir(exist_ok=True)
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps([sys.argv[1:], sorted(os.environ)]))\n"
        "sys.exit(0 if os.environ.get('AI_AGENT') else 7)\n"
    )
    cli.chmod(0o700)
    return cli, record


def test_check_auth_reads_the_token_with_the_launcher_parser_and_prints_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli, record = _fake_cli(tmp_path, "claude")
    token = tmp_path / "oauth-token.env"
    token.write_text(f"# day login\n#\nCLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    token.chmod(0o600)
    args = [
        "--principal",
        "claude@dev",
        "--home",
        str(tmp_path),
        "--cli",
        str(cli),
        "--token-file",
        str(token),
    ]

    code = check_auth(args, _identity_config(tmp_path))

    assert code == 0
    assert capsys.readouterr().out == "login ready\n"
    argv, names = json.loads(record.read_text())
    assert argv == ["auth", "status"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in names and "AI_AGENT" in names
    assert TOKEN not in record.read_text()


def test_check_auth_refuses_a_bad_token_file_without_running_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli, record = _fake_cli(tmp_path, "claude")
    args = [
        "--principal",
        "claude@dev",
        "--home",
        str(tmp_path),
        "--cli",
        str(cli),
        "--token-file",
        str(tmp_path / "absent.env"),
    ]

    assert check_auth(args, _identity_config(tmp_path)) == EXIT_PREFLIGHT
    assert capsys.readouterr().out == "refused: existing_token_unavailable\n"
    assert not record.exists()


def test_check_auth_for_pi_uses_its_status_check_and_refuses_a_token_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli, record = _fake_cli(tmp_path, "pi")
    base = ["--principal", "pi@dev", "--home", str(tmp_path), "--cli", str(cli)]

    assert check_auth([*base, "--provider", "openai"], _identity_config(tmp_path)) == 0
    assert json.loads(record.read_text())[0] == ["auth", "check", "--no-refresh", "--provider", "openai"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in json.loads(record.read_text())[1]
    assert (
        check_auth([*base, "--token-file", str(tmp_path / "t.env")], _identity_config(tmp_path))
        == EXIT_PREFLIGHT
    )
    assert capsys.readouterr().out.splitlines()[-1] == "refused: token_file_unsupported_backend"


@pytest.mark.parametrize(
    ("code", "outcome"), [(0, "native exit 0"), (3, "native exit 3"), (None, "budget expired, cli killed")]
)
def test_the_native_outcome_is_logged_without_any_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, code: int | None, outcome: str
) -> None:
    settings, host = _prepared(tmp_path), Host(code)
    with caplog.at_level("INFO", logger="agent_board.night.cli_launch"):
        launch(settings, host=host, assignments=Assignments(_assignment()), repositories=Repositories())

    lines = [
        record.getMessage() for record in caplog.records if record.name == "agent_board.night.cli_launch"
    ]
    assert lines == [f"night run {RUN} for pi@dev, configured backend pi: {outcome}"]
    text = caplog.text
    assert "work the held wakes" not in text and str(settings.cli) not in text and str(tmp_path) not in text


def test_a_refused_launch_logs_no_native_outcome(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _prepared(tmp_path)
    settings.workdir.mkdir()
    with caplog.at_level("INFO", logger="agent_board.night.cli_launch"), pytest.raises(LaunchRefused):
        launch(settings, host=Host(), assignments=Assignments(_assignment()), repositories=Repositories())

    assert "native exit" not in caplog.text and "budget expired" not in caplog.text
