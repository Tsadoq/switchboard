import logging
import math
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_board.night.assignment import Assignment
from agent_board.night.environment import (
    CLI_ENV,
    DAY_UID_ENV,
    DEADLINE_UTC_ENV,
    LOGIN_ENV,
    NIGHT_UID_ENV,
    PRINCIPAL_ENV,
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

    @property
    def workdir(self) -> Path:
        return self.workdir_root / self.run_id


class CliHost(Protocol):
    def uid(self) -> int: ...

    def containers_trusted(self, path: Path) -> bool: ...

    def clone(self, source: Path, destination: Path, commit: str) -> None: ...

    def run_cli(self, argv: Sequence[str], cwd: Path, timeout: float) -> int | None: ...

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
    return CliSettings(run_id, environ[PRINCIPAL_ENV], uid, home, workdir_root, cli, deadline_utc)


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
    source = repositories.resolve(assignment.repository_key, settings.principal)
    budget = min(
        float(assignment.turn_seconds), min(settings.deadline_utc, assignment.deadline_utc) - host.utc()
    )
    if budget <= 0:
        raise LaunchRefused(EXIT_BUDGET, "run_deadline_passed")
    host.clone(source, settings.workdir, assignment.base_commit)
    code = host.run_cli(argv, settings.workdir, budget)
    if code is None:
        return EXIT_BUDGET
    return EXIT_DONE if code == 0 else EXIT_CLI_FAILED


class LiveCliHost:
    def __init__(self, settings: CliSettings) -> None:
        self._env = {
            "HOME": str(settings.home),
            "PATH": f"{settings.cli.parent}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }

    def uid(self) -> int:
        return os.getuid()

    def containers_trusted(self, path: Path) -> bool:
        return trusted_containers(path)

    def clone(self, source: Path, destination: Path, commit: str) -> None:
        clone_mirror(source, destination, commit, self._git)

    def run_cli(self, argv: Sequence[str], cwd: Path, timeout: float) -> int | None:
        """The CLI's exit code; a run cut off at its budget is killed with its process group and reports None."""
        try:
            process = subprocess.Popen(
                list(argv), cwd=cwd, env=self._env, stdin=subprocess.DEVNULL, start_new_session=True
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

    def _git(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                list(argv),
                env={**self._env, **GIT_SAFETY_ENV},
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
