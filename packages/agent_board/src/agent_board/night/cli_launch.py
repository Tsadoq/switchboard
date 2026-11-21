import argparse
import json
import logging
import math
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_board.identity import identity_conf
from agent_board.night.assignment import Assignment
from agent_board.night.environment import (
    CLI_ENV,
    DAY_UID_ENV,
    DEADLINE_UTC_ENV,
    LOGIN_ENV,
    NIGHT_UID_ENV,
    PRINCIPAL_ENV,
    TOKEN_FILE_ENV,
    WORKDIR_ROOT_ENV,
)
from agent_board.night.launch import (
    _UUID,
    COMMAND_SECONDS,
    EXIT_ASSIGNMENT,
    EXIT_BUDGET,
    EXIT_DONE,
    EXIT_PREFLIGHT,
    EXIT_SESSION,
    EXIT_USAGE,
    GIT_SAFETY_ENV,
    AssignmentSource,
    LaunchRefused,
    RepositoryPolicy,
    SealedAssignments,
    _absolute,
    _mirror_policy,
    _positive_id,
    _private_directory,
    clone_mirror,
    trusted_containers,
)

EXISTING_LOGIN = "existing"
EXIT_CLI_FAILED = 8
CHECK_AUTH = "--check-auth"
_CHECK_RUN = "00000000-0000-4000-8000-000000000000"
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
MAX_TOKEN_FILE_BYTES = 4096
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{16,2048}")
AGENT_ENV = "AI_AGENT"
CLAUDE_PERMISSION_MODES = {"read_only": "plan", "default": "dontAsk", "auto": "auto"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliSettings:
    run_id: str
    principal: str
    uid: int
    home: Path
    workdir_root: Path
    cli: Path
    deadline_utc: float
    token_file: Path | None = None

    @property
    def workdir(self) -> Path:
        return self.workdir_root / self.run_id


class CliHost(Protocol):
    def uid(self) -> int: ...

    def containers_trusted(self, path: Path) -> bool: ...

    def clone(self, source: Path, destination: Path, commit: str) -> None: ...

    def run_cli(
        self, argv: Sequence[str], cwd: Path, timeout: float, extra_env: Mapping[str, str]
    ) -> int | None: ...

    def utc(self) -> float: ...


def cli_settings_from(run_id: str, environ: Mapping[str, str], uid: int) -> CliSettings:
    """The exec step set this environment from the root-owned config and journal; none of it comes from the agent."""
    if _UUID.fullmatch(run_id) is None:
        raise LaunchRefused(EXIT_USAGE, "invalid_run_id")
    if environ.get(LOGIN_ENV) != EXISTING_LOGIN:
        raise LaunchRefused(EXIT_PREFLIGHT, "not_an_existing_login_run")
    names = (PRINCIPAL_ENV, "HOME", WORKDIR_ROOT_ENV, CLI_ENV, DEADLINE_UTC_ENV)
    missing = [name for name in names if name not in environ]
    if missing:
        raise LaunchRefused(EXIT_PREFLIGHT, f"missing_environment:{missing[0]}")
    home, workdir_root, cli = (_absolute(environ[name]) for name in ("HOME", WORKDIR_ROOT_ENV, CLI_ENV))
    if home is None or workdir_root is None or cli is None:
        raise LaunchRefused(EXIT_PREFLIGHT, "unsafe_path_in_environment")
    try:
        deadline_utc = float(environ[DEADLINE_UTC_ENV])
    except ValueError:
        deadline_utc = math.nan
    if not math.isfinite(deadline_utc):
        raise LaunchRefused(EXIT_PREFLIGHT, "invalid_deadline")
    exec_uid, day_uid = (_positive_id(environ.get(name)) for name in (NIGHT_UID_ENV, DAY_UID_ENV))
    if exec_uid != uid or day_uid != uid:
        raise LaunchRefused(EXIT_PREFLIGHT, "wrong_uid")
    token_file = None
    if TOKEN_FILE_ENV in environ:
        token_file = _absolute(environ[TOKEN_FILE_ENV])
        if token_file is None:
            raise LaunchRefused(EXIT_PREFLIGHT, "unsafe_path_in_environment")
    return CliSettings(run_id, environ[PRINCIPAL_ENV], uid, home, workdir_root, cli, deadline_utc, token_file)


def preflight(settings: CliSettings, host: CliHost) -> None:
    """Runs only after the drop: the CLI is the account's own, so it is checked as that account, never by root."""
    if host.uid() != settings.uid or settings.uid <= 0:
        raise LaunchRefused(EXIT_PREFLIGHT, "wrong_uid")
    if not host.containers_trusted(settings.workdir_root):
        raise LaunchRefused(EXIT_PREFLIGHT, "workdir_root_container_not_root_managed")
    _private_directory(settings.workdir_root, settings.uid, "workdir_root")
    if os.path.lexists(settings.workdir):
        raise LaunchRefused(EXIT_PREFLIGHT, "workdir_exists")
    try:
        metadata = settings.cli.stat()
    except OSError:
        raise LaunchRefused(EXIT_PREFLIGHT, "cli_missing") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(settings.cli, os.X_OK):
        raise LaunchRefused(EXIT_PREFLIGHT, "cli_not_executable")


def cli_argv(settings: CliSettings, assignment: Assignment) -> list[str]:
    """The non-interactive CLI call for the configured backend; a backend this CLI is not refuses."""
    if settings.cli.name != assignment.backend:
        raise LaunchRefused(EXIT_ASSIGNMENT, "backend_does_not_match_cli")
    argv = [str(settings.cli), "-p", assignment.prompt]
    if assignment.model is not None:
        argv += ["--model", assignment.model]
    if assignment.backend == "claude":
        argv += ["--permission-mode", CLAUDE_PERMISSION_MODES[assignment.permission_mode]]
    elif assignment.permission_mode != "auto":
        raise LaunchRefused(EXIT_ASSIGNMENT, "permission_mode_unsupported_by_backend")
    return argv


def existing_token(settings: CliSettings, assignment: Assignment, uid: int) -> dict[str, str]:
    """The account's own Claude token, read only after the drop and passed as that one variable alone.

    The file is the account's existing systemd environment file. It is parsed, never sourced, and every other
    key in it is ignored; a missing, foreign, malformed or empty token refuses before the model starts.
    """
    if settings.token_file is None:
        return {}
    if assignment.backend != "claude":
        raise LaunchRefused(EXIT_PREFLIGHT, "token_file_unsupported_backend")
    return {TOKEN_VARIABLE: read_token_file(settings.token_file, uid)}


def read_token_file(path: Path, uid: int) -> str:
    """CLAUDE_CODE_OAUTH_TOKEN from the account's own environment file, parsed as KEY=VALUE lines, never sourced."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise LaunchRefused(EXIT_PREFLIGHT, "existing_token_unavailable") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid or metadata.st_mode & 0o022:
            raise LaunchRefused(EXIT_PREFLIGHT, "existing_token_not_the_accounts")
        if metadata.st_size > MAX_TOKEN_FILE_BYTES:
            raise LaunchRefused(EXIT_PREFLIGHT, "existing_token_malformed")
        data = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise LaunchRefused(EXIT_PREFLIGHT, "existing_token_malformed") from None
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or _ENV_KEY.fullmatch(key.strip()) is None:
            raise LaunchRefused(EXIT_PREFLIGHT, "existing_token_malformed")
        if key.strip() == TOKEN_VARIABLE:
            values.append(_unquoted(value.strip()))
    if len(values) != 1 or _TOKEN.fullmatch(values[0]) is None:
        raise LaunchRefused(EXIT_PREFLIGHT, "existing_token_malformed")
    return values[0]


def _unquoted(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def launch(
    settings: CliSettings, *, host: CliHost, assignments: AssignmentSource, repositories: RepositoryPolicy
) -> int:
    preflight(settings, host)
    try:
        assignment = assignments.fetch(settings.run_id, settings.principal)
    except LaunchRefused:
        raise
    except (OSError, ValueError) as error:
        raise LaunchRefused(EXIT_ASSIGNMENT, f"assignment_unavailable:{type(error).__name__}") from None
    if assignment.run_id != settings.run_id or assignment.principal != settings.principal:
        raise LaunchRefused(EXIT_ASSIGNMENT, "assignment_mismatch")
    argv = cli_argv(settings, assignment)
    extra_env = existing_token(settings, assignment, host.uid())
    source = repositories.resolve(assignment.repository_key, settings.principal)
    deadline = min(settings.deadline_utc, assignment.deadline_utc)
    started = host.utc()
    if deadline - started <= 0:
        raise LaunchRefused(EXIT_BUDGET, "run_deadline_passed")
    host.clone(source, settings.workdir, assignment.base_commit)
    now = host.utc()
    budget = min(float(assignment.turn_seconds) - (now - started), deadline - now)
    if budget <= 0:
        raise LaunchRefused(EXIT_BUDGET, "run_deadline_passed_during_setup")
    code = host.run_cli(argv, settings.workdir, budget, extra_env)
    outcome = "budget expired, cli killed" if code is None else f"native exit {code}"
    logger.info(
        "night run %s for %s, configured backend %s: %s",
        settings.run_id,
        settings.principal,
        assignment.backend,
        outcome,
    )
    if code is None:
        return EXIT_BUDGET
    return EXIT_DONE if code == 0 else EXIT_CLI_FAILED


def ai_agent_for(principal: str, conf: Path) -> str:
    """The AI_AGENT value the box's identity config gives the principal's kind, so board tools sign as it."""
    kind = principal.partition("@")[0]
    try:
        configured = json.loads(conf.read_text())["kinds"]
        values = [entry["ai_agent"] for entry in configured if entry["name"] == kind]
    except (OSError, ValueError, KeyError, TypeError):
        raise LaunchRefused(EXIT_PREFLIGHT, "identity_config_unreadable") from None
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise LaunchRefused(EXIT_PREFLIGHT, "identity_kind_not_configured")
    return values[0]


class LiveCliHost:
    def __init__(self, settings: CliSettings, identity_config: Path | None = None) -> None:
        tools = dict.fromkeys(
            (
                str(settings.cli.parent),
                str(settings.home / ".local" / "bin"),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            )
        )
        self._env = {
            "HOME": str(settings.home),
            "PATH": ":".join(tools),
            "LANG": "C.UTF-8",
            AGENT_ENV: ai_agent_for(settings.principal, identity_config or identity_conf()),
        }

    def uid(self) -> int:
        return os.getuid()

    def containers_trusted(self, path: Path) -> bool:
        return trusted_containers(path)

    def clone(self, source: Path, destination: Path, commit: str) -> None:
        clone_mirror(source, destination, commit, self._git)

    def run_cli(
        self, argv: Sequence[str], cwd: Path, timeout: float, extra_env: Mapping[str, str]
    ) -> int | None:
        """The CLI's exit code; a run cut off at its budget is killed with its process group and reports None."""
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env={**self._env, **extra_env},
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise LaunchRefused(EXIT_SESSION, f"cli_failed_to_start:{type(error).__name__}") from None
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 9)
            process.wait()
            return None

    def utc(self) -> float:
        return time.time()

    def run_status(self, argv: Sequence[str], extra_env: Mapping[str, str]) -> int:
        """A CLI status command with its output discarded, so no credential detail can be printed."""
        try:
            completed = subprocess.run(
                list(argv),
                env={**self._env, **extra_env},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=COMMAND_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return EXIT_PREFLIGHT
        return completed.returncode

    def _git(self, argv: Sequence[str], extra: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                list(argv),
                env={**self._env, **GIT_SAFETY_ENV, **extra},
                capture_output=True,
                text=True,
                timeout=COMMAND_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaunchRefused(EXIT_PREFLIGHT, f"command_failed:{type(error).__name__}") from None
        if completed.returncode != 0:
            raise LaunchRefused(EXIT_ASSIGNMENT, f"git_exit_{completed.returncode}")
        return completed


def main(
    argv: Sequence[str],
    environ: Mapping[str, str] = os.environ,
    host: Callable[[CliSettings], CliHost] = LiveCliHost,
    assignments: Callable[[CliSettings], AssignmentSource] = lambda settings: SealedAssignments(os.environ),
    repositories: Callable[[CliSettings], RepositoryPolicy] = lambda settings: _mirror_policy(),
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if argv[:1] == [CHECK_AUTH]:
        return check_auth(argv[1:])
    if len(argv) != 1:
        return EXIT_USAGE
    try:
        settings = cli_settings_from(argv[0], environ, os.getuid())
        return launch(
            settings,
            host=host(settings),
            assignments=assignments(settings),
            repositories=repositories(settings),
        )
    except LaunchRefused as refused:
        logger.error("night launch refused: %s", refused)
        return refused.exit_code


def check_auth(argv: Sequence[str], identity_config: Path | None = None) -> int:
    """A read-only login check, run as the account: the launcher's environment and token parser, no model call.

    It prints only "login ready" or "refused: <reason>", never a credential. The exit status is the CLI's own
    status check, or EXIT_PREFLIGHT when the launcher itself would refuse.
    """
    parser = argparse.ArgumentParser(prog="cli_launch --check-auth")
    parser.add_argument("--principal", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--token-file")
    parser.add_argument("--provider")
    args = parser.parse_args(list(argv))
    home, cli = _absolute(args.home), _absolute(args.cli)
    token_file = None if args.token_file is None else _absolute(args.token_file)
    try:
        if home is None or cli is None or (args.token_file is not None and token_file is None):
            raise LaunchRefused(EXIT_PREFLIGHT, "unsafe_path_in_arguments")
        if cli.name not in ("claude", "pi"):
            raise LaunchRefused(EXIT_PREFLIGHT, "unsupported_cli")
        if token_file is not None and cli.name != "claude":
            raise LaunchRefused(EXIT_PREFLIGHT, "token_file_unsupported_backend")
        uid = os.getuid()
        settings = CliSettings(_CHECK_RUN, args.principal, uid, home, home, cli, math.inf, token_file)
        host = LiveCliHost(settings, identity_config)
        extra = {} if token_file is None else {TOKEN_VARIABLE: read_token_file(token_file, uid)}
    except LaunchRefused as refused:
        print(f"refused: {refused}")
        return EXIT_PREFLIGHT
    if cli.name == "claude":
        status = [str(cli), "auth", "status"]
    else:
        status = [
            str(cli),
            "auth",
            "check",
            "--no-refresh",
            *(["--provider", args.provider] if args.provider else []),
        ]
    code = host.run_status(status, extra)
    print("login ready" if code == 0 else f"refused: status exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
