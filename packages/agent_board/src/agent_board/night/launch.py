import json
import logging
import math
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_board.night.environment import (
    DEADLINE_UTC_ENV,
    HAPPIER_BIN_ENV,
    HOME_DIR_ENV,
    PRINCIPAL_ENV,
    WORKDIR_ROOT_ENV,
)
from agent_board.runner.happier import Happier, HappierError

EXIT_DONE = 0
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3
EXIT_ASSIGNMENT = 4
EXIT_DAEMON = 5
EXIT_SESSION = 6
EXIT_BUDGET = 7

DAY_HOMES = (".happier", ".happier-board")
BACKENDS = ("claude",)
PERMISSION_MODES = ("read_only", "default", "auto")
MAX_PROMPT_CHARS = 32_000
MAX_TURN_SECONDS = 3600
DAEMON_READY_SECONDS = 60.0
SESSION_TRACKED_SECONDS = 30.0
DAEMON_STOP_SECONDS = 15.0
COMMAND_SECONDS = 120
POLL_SECONDS = 1.0
IDLE_PROBE_SECONDS = 30
GIT = "/usr/bin/git"
NO_SESSIONS = "This daemon has no tracked active sessions."
SESSIONS_HEADER = "Active sessions:"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
logger = logging.getLogger(__name__)


class LaunchRefused(RuntimeError):
    def __init__(self, exit_code: int, reason: str) -> None:
        super().__init__(reason)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Settings:
    run_id: str
    principal: str
    uid: int
    happier_home: Path
    home: Path
    runtime_dir: Path
    workdir_root: Path
    happier_bin: Path
    deadline_utc: float

    @property
    def workdir(self) -> Path:
        return self.workdir_root / self.run_id


@dataclass(frozen=True)
class Assignment:
    """What one night run does. It names no path, URL or command: the repository is a key the policy resolves."""

    run_id: str
    principal: str
    repository_key: str
    base_commit: str
    prompt: str
    backend: str
    model: str | None
    permission_mode: str
    turn_seconds: int

    def __post_init__(self) -> None:
        if _REPOSITORY_KEY.fullmatch(self.repository_key) is None:
            raise ValueError("repository_key is not a policy key")
        if _COMMIT.fullmatch(self.base_commit) is None:
            raise ValueError("base_commit must be a full lowercase commit id")
        if not self.prompt or len(self.prompt) > MAX_PROMPT_CHARS:
            raise ValueError("prompt must be non-empty and bounded")
        if self.backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}")
        if self.model is not None and _MODEL.fullmatch(self.model) is None:
            raise ValueError("model is not a model id")
        if self.permission_mode not in PERMISSION_MODES:
            raise ValueError(f"permission_mode must be one of {PERMISSION_MODES}")
        if type(self.turn_seconds) is not int or not 1 <= self.turn_seconds <= MAX_TURN_SECONDS:
            raise ValueError("turn_seconds must be an integer within the turn bound")


class AssignmentSource(Protocol):
    def fetch(self, run_id: str, principal: str) -> Assignment:
        """The fenced server delivery of this run's assignment; raises when there is none."""
        ...


class RepositoryPolicy(Protocol):
    def resolve(self, repository_key: str, principal: str) -> Path:
        """The reviewed clone source for a key; raises LaunchRefused for any key outside the policy."""
        ...


@dataclass(frozen=True)
class TrackedSession:
    session_id: str
    pid: int


class Daemon(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...


class Sessions(Protocol):
    def create(
        self,
        *,
        backend: str,
        path: Path,
        tag: str,
        title: str,
        prompt: str,
        permission_mode: str,
        model: str | None = None,
    ) -> Any: ...

    def idle(self, session_id: str, timeout: int = 3) -> bool: ...

    def archive(self, session_id: str) -> None: ...


class Host(Protocol):
    """The side effects the launcher has, so every step is observable in tests."""

    def uid(self) -> int: ...

    def account_home(self, uid: int) -> Path: ...

    def release_trusted(self, binary: Path) -> bool: ...

    def daemon_status(self) -> Mapping[str, Any]: ...

    def daemon_sessions(self) -> list[TrackedSession]: ...

    def own_cgroup(self) -> str: ...

    def process_cgroup(self, pid: int) -> str | None: ...

    def start_daemon(self) -> Daemon: ...

    def stop_daemon(self) -> None: ...

    def clone(self, source: Path, destination: Path, commit: str) -> None: ...

    def monotonic(self) -> float: ...

    def utc(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


def settings_from(run_id: str, environ: Mapping[str, str], uid: int) -> Settings:
    """The exec step set this environment from the root-owned config and journal; none of it comes from the agent."""
    if _UUID.fullmatch(run_id) is None:
        raise LaunchRefused(EXIT_USAGE, "invalid_run_id")
    names = (PRINCIPAL_ENV, HOME_DIR_ENV, "HOME", "XDG_RUNTIME_DIR", WORKDIR_ROOT_ENV, HAPPIER_BIN_ENV)
    missing = [name for name in (*names, DEADLINE_UTC_ENV) if name not in environ]
    if missing:
        raise LaunchRefused(EXIT_PREFLIGHT, f"missing_environment:{missing[0]}")
    paths = {name: _absolute(environ[name]) for name in names if name != PRINCIPAL_ENV}
    if any(path is None for path in paths.values()):
        raise LaunchRefused(EXIT_PREFLIGHT, "unsafe_path_in_environment")
    try:
        deadline_utc = float(environ[DEADLINE_UTC_ENV])
    except ValueError:
        deadline_utc = math.nan
    if not math.isfinite(deadline_utc):
        raise LaunchRefused(EXIT_PREFLIGHT, "invalid_deadline")
    return Settings(
        run_id=run_id,
        principal=environ[PRINCIPAL_ENV],
        uid=uid,
        happier_home=Path(environ[HOME_DIR_ENV]),
        home=Path(environ["HOME"]),
        runtime_dir=Path(environ["XDG_RUNTIME_DIR"]),
        workdir_root=Path(environ[WORKDIR_ROOT_ENV]),
        happier_bin=Path(environ[HAPPIER_BIN_ENV]),
        deadline_utc=deadline_utc,
    )


def preflight(settings: Settings, host: Host) -> None:
    """Refuses, before any process starts or any file changes, unless the night home is private, separate
    from the account's day homes, authenticated by Happier's own status, and served by no other daemon."""
    if host.uid() != settings.uid or settings.uid <= 0:
        raise LaunchRefused(EXIT_PREFLIGHT, "wrong_uid")
    if not host.release_trusted(settings.happier_bin):
        raise LaunchRefused(EXIT_PREFLIGHT, "happier_release_not_root_owned")
    _private_directory(settings.happier_home, settings.uid, "happier_home")
    _private_directory(settings.workdir_root, settings.uid, "workdir_root")
    for inner in (settings.home, settings.runtime_dir):
        if not inner.is_relative_to(settings.happier_home) or inner == settings.happier_home:
            raise LaunchRefused(EXIT_PREFLIGHT, f"{inner} is not inside happier_home")
    account_home = host.account_home(settings.uid)
    for day in (account_home / name for name in DAY_HOMES):
        if _overlaps(settings.happier_home, day):
            raise LaunchRefused(EXIT_PREFLIGHT, f"happier_home overlaps the day home {day}")
    if _overlaps(settings.happier_home, account_home) or _overlaps(settings.workdir_root, account_home):
        raise LaunchRefused(EXIT_PREFLIGHT, "a night path overlaps the account home")
    if os.path.lexists(settings.workdir):
        raise LaunchRefused(EXIT_PREFLIGHT, "workdir_exists")
    status = host.daemon_status()
    auth, daemon, service = (_mapping(status, key) for key in ("auth", "daemon", "service"))
    if auth.get("authenticated") is not True or auth.get("machineRegistered") is not True:
        raise LaunchRefused(EXIT_PREFLIGHT, "happier_home_not_authenticated")
    if daemon.get("running") is not False or service.get("installed") is not False:
        raise LaunchRefused(EXIT_PREFLIGHT, "happier_home_has_another_daemon")


def launch(
    settings: Settings,
    *,
    host: Host,
    assignments: AssignmentSource,
    repositories: RepositoryPolicy,
    sessions: Sessions,
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
    source = repositories.resolve(assignment.repository_key, settings.principal)
    budget = min(float(assignment.turn_seconds), settings.deadline_utc - host.utc())
    if budget <= 0:
        raise LaunchRefused(EXIT_BUDGET, "run_deadline_passed")
    deadline = host.monotonic() + budget
    for directory in (settings.home, settings.runtime_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
    host.clone(source, settings.workdir, assignment.base_commit)
    daemon = host.start_daemon()
    try:
        _await_contained_daemon(host, daemon)
        return _run_session(settings, assignment, host, daemon, sessions, deadline)
    finally:
        _stop_daemon(host, daemon)


def _await_contained_daemon(host: Host, daemon: Daemon) -> None:
    """Ready means this process, the one just spawned, answers as the home's daemon from the run's own cgroup."""
    own = host.own_cgroup()
    deadline = host.monotonic() + DAEMON_READY_SECONDS
    while host.monotonic() < deadline:
        if daemon.poll() is not None:
            raise LaunchRefused(EXIT_DAEMON, "daemon_exited")
        status = host.daemon_status()
        reported = _mapping(status, "daemon")
        if (
            reported.get("running") is True
            and _mapping(status, "runtimeConvergence").get("controlReachable") is True
        ):
            if reported.get("pid") != daemon.pid:
                raise LaunchRefused(EXIT_DAEMON, "unexpected_daemon_identity")
            if host.process_cgroup(daemon.pid) != own:
                raise LaunchRefused(EXIT_DAEMON, "daemon_outside_run_cgroup")
            return
        host.sleep(POLL_SECONDS)
    raise LaunchRefused(EXIT_DAEMON, "daemon_not_ready")


def _run_session(
    settings: Settings,
    assignment: Assignment,
    host: Host,
    daemon: Daemon,
    sessions: Sessions,
    deadline: float,
) -> int:
    try:
        session = sessions.create(
            backend=assignment.backend,
            path=settings.workdir,
            tag=f"night:{settings.run_id}",
            title=f"night run {settings.run_id}",
            prompt=assignment.prompt,
            permission_mode=assignment.permission_mode,
            model=assignment.model,
        )
    except HappierError as error:
        raise LaunchRefused(EXIT_SESSION, f"session_create_failed:{error.code}") from None
    try:
        _require_contained_session(host, session.id)
        while True:
            if daemon.poll() is not None:
                raise LaunchRefused(EXIT_DAEMON, "daemon_exited")
            remaining = deadline - host.monotonic()
            if remaining <= 0:
                return EXIT_BUDGET
            try:
                if sessions.idle(session.id, timeout=max(1, min(IDLE_PROBE_SECONDS, int(remaining)))):
                    return EXIT_DONE
            except HappierError as error:
                raise LaunchRefused(EXIT_SESSION, f"session_wait_failed:{error.code}") from None
    finally:
        try:
            sessions.archive(session.id)
        except HappierError as error:
            logger.warning("archiving night session %s failed: %s", session.id, error.code)


def _require_contained_session(host: Host, session_id: str) -> None:
    """The daemon must track exactly this one session, and its process must sit in the run's cgroup."""
    own = host.own_cgroup()
    deadline = host.monotonic() + SESSION_TRACKED_SECONDS
    while host.monotonic() < deadline:
        tracked = host.daemon_sessions()
        if tracked:
            if [each.session_id for each in tracked] != [session_id]:
                raise LaunchRefused(EXIT_SESSION, "unexpected_tracked_sessions")
            if host.process_cgroup(tracked[0].pid) != own:
                raise LaunchRefused(EXIT_SESSION, "session_outside_run_cgroup")
            return
        host.sleep(POLL_SECONDS)
    raise LaunchRefused(EXIT_SESSION, "session_not_tracked")


def _stop_daemon(host: Host, daemon: Daemon) -> None:
    try:
        host.stop_daemon()
    except LaunchRefused as error:
        logger.warning("stopping the night daemon failed: %s", error)
    deadline = host.monotonic() + DAEMON_STOP_SECONDS
    while daemon.poll() is None and host.monotonic() < deadline:
        host.sleep(POLL_SECONDS)


def release_trusted(binary: Path, owner: int = 0) -> bool:
    """The binary's whole version directory: Happier's ELF loads its bundled modules from beside itself."""
    release = binary.parent
    try:
        for each in (release, *release.parents):
            if not _owned_unwritable(each.lstat(), owner, directory=True):
                return False
        if not _owned_unwritable(binary.lstat(), owner, directory=False):
            return False
        for root, directories, files in os.walk(release):
            for name in (*directories, *files):
                if not _owned_unwritable(Path(root, name).lstat(), owner, directory=None):
                    return False
    except OSError:
        return False
    return True


def _owned_unwritable(metadata: os.stat_result, owner: int, directory: bool | None) -> bool:
    if metadata.st_uid != owner:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return directory is None
    if directory is True and not stat.S_ISDIR(metadata.st_mode):
        return False
    if directory is False and not stat.S_ISREG(metadata.st_mode):
        return False
    return not metadata.st_mode & 0o022


def tracked_sessions(output: str) -> list[TrackedSession]:
    """Parses `happier daemon list`, which prints a fixed sentence when empty, else a header and a JSON array."""
    text = output.strip()
    if text.startswith(NO_SESSIONS):
        return []
    header, _, body = text.partition("\n")
    if header.strip() != SESSIONS_HEADER:
        raise LaunchRefused(EXIT_SESSION, "daemon_list_unreadable")
    try:
        entries = json.loads(body)
    except ValueError:
        raise LaunchRefused(EXIT_SESSION, "daemon_list_unreadable") from None
    if not isinstance(entries, list):
        raise LaunchRefused(EXIT_SESSION, "daemon_list_unreadable")
    result = []
    for entry in entries:
        session_id = entry.get("happySessionId") if isinstance(entry, dict) else None
        pid = entry.get("pid") if isinstance(entry, dict) else None
        if not isinstance(session_id, str) or not session_id or type(pid) is not int or pid <= 0:
            raise LaunchRefused(EXIT_SESSION, "daemon_list_unreadable")
        result.append(TrackedSession(session_id, pid))
    return result


def _private_directory(path: Path, uid: int, name: str) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise LaunchRefused(EXIT_PREFLIGHT, f"{name}_missing") from None
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid or metadata.st_mode & 0o077:
        raise LaunchRefused(EXIT_PREFLIGHT, f"{name}_not_private")


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _mapping(status: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = status.get(key)
    if not isinstance(value, Mapping):
        raise LaunchRefused(EXIT_PREFLIGHT, f"daemon_status_without_{key}")
    return value


def _absolute(value: str) -> Path | None:
    if not value.startswith("/") or os.path.normpath(value) != value or "\x00" in value:
        return None
    return Path(value)


class LiveDaemon:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()


class LiveHost:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._env = {
            "HOME": str(settings.home),
            "XDG_RUNTIME_DIR": str(settings.runtime_dir),
            HOME_DIR_ENV: str(settings.happier_home),
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HAPPIER_CLI_UPDATE_CHECK": "0",
            "HAPPIER_FEATURE_BUG_REPORTS__ENABLED": "0",
        }
        self._git_env = {
            **self._env,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_PROTOCOL_FROM_USER": "0",
        }

    def uid(self) -> int:
        return os.getuid()

    def account_home(self, uid: int) -> Path:
        return Path(pwd.getpwuid(uid).pw_dir)

    def release_trusted(self, binary: Path) -> bool:
        return release_trusted(binary)

    def daemon_status(self) -> Mapping[str, Any]:
        completed = self._run([str(self._settings.happier_bin), "daemon", "status", "--json"])
        try:
            status = json.loads(completed.stdout)
        except ValueError:
            raise LaunchRefused(EXIT_PREFLIGHT, "daemon_status_unreadable") from None
        if completed.returncode != 0 or not isinstance(status, dict):
            raise LaunchRefused(EXIT_PREFLIGHT, "daemon_status_unreadable")
        return status

    def daemon_sessions(self) -> list[TrackedSession]:
        completed = self._run([str(self._settings.happier_bin), "daemon", "list"])
        if completed.returncode != 0:
            raise LaunchRefused(EXIT_SESSION, "daemon_list_failed")
        return tracked_sessions(completed.stdout)

    def own_cgroup(self) -> str:
        return Path("/proc/self/cgroup").read_text().strip()

    def process_cgroup(self, pid: int) -> str | None:
        try:
            return Path(f"/proc/{pid}/cgroup").read_text().strip()
        except OSError:
            return None

    def start_daemon(self) -> Daemon:
        process = subprocess.Popen(
            [str(self._settings.happier_bin), "daemon", "start-sync"],
            env=self._env,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        return LiveDaemon(process)

    def stop_daemon(self) -> None:
        self._run([str(self._settings.happier_bin), "daemon", "stop", "--kill-sessions"])

    def clone(self, source: Path, destination: Path, commit: str) -> None:
        local_only = ("-c", "protocol.allow=never", "-c", "protocol.file.allow=always")
        hooks_off = ("-c", "core.hooksPath=/dev/null", *local_only)
        self._git(
            [GIT, *hooks_off, "clone", "--quiet", "--no-hardlinks", "--no-checkout", "--template="]
            + ["--", str(source), str(destination)]
        )
        self._git([GIT, *hooks_off, "-C", str(destination), "checkout", "--quiet", "--detach", commit])
        head = self._git([GIT, "-C", str(destination), "rev-parse", "HEAD"]).stdout.strip()
        if head != commit:
            raise LaunchRefused(EXIT_ASSIGNMENT, "checkout_mismatch")

    def monotonic(self) -> float:
        return time.monotonic()

    def utc(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _run(
        self, argv: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                env=dict(env or self._env),
                capture_output=True,
                text=True,
                timeout=COMMAND_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaunchRefused(EXIT_PREFLIGHT, f"command_failed:{type(error).__name__}") from None

    def _git(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        completed = self._run(argv, self._git_env)
        if completed.returncode != 0:
            raise LaunchRefused(EXIT_ASSIGNMENT, f"git_exit_{completed.returncode}")
        return completed


class UnavailableAssignments:
    """Stands in until the fenced server delivery of assignments exists, so a real launch refuses."""

    def fetch(self, run_id: str, principal: str) -> Assignment:
        raise LaunchRefused(EXIT_ASSIGNMENT, "assignment_source_not_configured")


class NoRepositories:
    """Stands in until the reviewed repository policy exists, so no key resolves."""

    def resolve(self, repository_key: str, principal: str) -> Path:
        raise LaunchRefused(EXIT_ASSIGNMENT, "repository_policy_not_configured")


def main(
    argv: Sequence[str],
    environ: Mapping[str, str] = os.environ,
    assignments: Callable[[Settings], AssignmentSource] = lambda settings: UnavailableAssignments(),
    repositories: Callable[[Settings], RepositoryPolicy] = lambda settings: NoRepositories(),
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(argv) != 1:
        return EXIT_USAGE
    try:
        settings = settings_from(argv[0], environ, os.getuid())
        return launch(
            settings,
            host=LiveHost(settings),
            assignments=assignments(settings),
            repositories=repositories(settings),
            sessions=Happier(str(settings.happier_bin), session_home=settings.happier_home),
        )
    except LaunchRefused as refused:
        logger.error("night launch refused: %s", refused)
        return refused.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
