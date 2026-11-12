import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board.night.assignment import Assignment
from agent_board.night.environment import (
    DEADLINE_UTC_ENV,
    HAPPIER_BIN_ENV,
    PLAIN_TERMINAL,
    PRINCIPAL_ENV,
    WORKDIR_ROOT_ENV,
)
from agent_board.night.launch import (
    EXIT_ASSIGNMENT,
    EXIT_BUDGET,
    EXIT_DAEMON,
    EXIT_DONE,
    EXIT_PREFLIGHT,
    EXIT_SESSION,
    EXIT_USAGE,
    LaunchRefused,
    Settings,
    TrackedSession,
    launch,
    preflight,
    release_trusted,
    settings_from,
    tracked_sessions,
    trusted_containers,
)
from agent_board.runner.happier import Happier, HappierError, Session

PRINCIPAL = "claude@dev"
COMMIT = "a" * 40
DAEMON_PID = 4242
SESSION_PID = 4343
OWN_CGROUP = "0::/switchboard.slice/switchboard-night.slice/switchboard-night-x.service"
NOW_UTC = 1_000_000.0


def _ready(pid: int = DAEMON_PID) -> dict[str, Any]:
    return {
        "auth": {"authenticated": True, "machineRegistered": True},
        "daemon": {"running": True, "pid": pid},
        "service": {"installed": False},
        "runtimeConvergence": {"controlReachable": True},
    }


IDLE_HOME = {
    **_ready(),
    "daemon": {"running": False, "pid": None},
    "runtimeConvergence": {"controlReachable": False},
}


class FakeDaemon:
    def __init__(self, exits_after: int | None = None) -> None:
        self.polls = 0
        self.exits_after = exits_after

    @property
    def pid(self) -> int:
        return DAEMON_PID

    def poll(self) -> int | None:
        self.polls += 1
        if self.exits_after is not None and self.polls > self.exits_after:
            return 1
        return None


class FakeHost:
    def __init__(self, account_home: Path) -> None:
        self.calls: list[str] = []
        self.statuses: list[Mapping[str, Any]] = [IDLE_HOME, _ready()]
        self.tracked: list[list[TrackedSession]] = [[], [TrackedSession("s-1", SESSION_PID)]]
        self.cgroups = {DAEMON_PID: OWN_CGROUP, SESSION_PID: OWN_CGROUP}
        self.daemon = FakeDaemon()
        self.now = 0.0
        self.trusted_release = True
        self._account_home = account_home
        self.cloned: tuple[Path, Path, str] | None = None

    def uid(self) -> int:
        return os.getuid()

    def account_home(self, uid: int) -> Path:
        return self._account_home

    def release_trusted(self, binary: Path) -> bool:
        return self.trusted_release

    def containers_trusted(self, path: Path) -> bool:
        return trusted_containers(path, owner=os.getuid(), top=self._account_home.parent)

    def daemon_status(self) -> Mapping[str, Any]:
        self.calls.append("status")
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def daemon_sessions(self) -> list[TrackedSession]:
        self.calls.append("list")
        return self.tracked.pop(0) if len(self.tracked) > 1 else self.tracked[0]

    def own_cgroup(self) -> str:
        return OWN_CGROUP

    def process_cgroup(self, pid: int) -> str | None:
        return self.cgroups.get(pid)

    def start_daemon(self) -> FakeDaemon:
        self.calls.append("start_daemon")
        return self.daemon

    def stop_daemon(self) -> None:
        self.calls.append("stop_daemon")
        self.daemon.exits_after = 0

    def clone(self, source: Path, destination: Path, commit: str) -> None:
        self.calls.append("clone")
        self.cloned = (source, destination, commit)

    def monotonic(self) -> float:
        return self.now

    def utc(self) -> float:
        return NOW_UTC + self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSessions:
    def __init__(self, host: FakeHost) -> None:
        self.host = host
        self.created: dict[str, Any] | None = None
        self.idle_after = 1
        self.waits = 0
        self.create_error: HappierError | None = None

    def create(self, **arguments: Any) -> Session:
        self.host.calls.append("create")
        if self.create_error is not None:
            raise self.create_error
        self.created = arguments
        return Session("s-1", True)

    def idle(self, session_id: str, timeout: int = 3) -> bool:
        self.host.calls.append("idle")
        self.waits += 1
        self.host.now += timeout
        return self.waits >= self.idle_after

    def archive(self, session_id: str) -> None:
        self.host.calls.append("archive")


@dataclass
class FakeAssignments:
    assignment: Assignment | None = None
    error: Exception | None = None

    def fetch(self, run_id: str, principal: str) -> Assignment:
        if self.error is not None:
            raise self.error
        assert self.assignment is not None
        return self.assignment


class FakeRepositories:
    def __init__(self, known: Mapping[str, Path]) -> None:
        self.known = known

    def resolve(self, repository_key: str, principal: str) -> Path:
        if repository_key not in self.known:
            raise LaunchRefused(EXIT_ASSIGNMENT, "unknown_repository")
        return self.known[repository_key]


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.account = tmp_path / "account"
        self.account.mkdir()
        night = tmp_path / "night"
        night.mkdir()
        happier_home = night / "happier"
        workdir_root = night / "work"
        for directory in (happier_home, workdir_root):
            directory.mkdir(mode=0o700)
        run_id = str(uuid4())
        self.settings = Settings(
            run_id=run_id,
            principal=PRINCIPAL,
            uid=os.getuid(),
            happier_home=happier_home,
            home=happier_home / "home",
            runtime_dir=happier_home / "run",
            workdir_root=workdir_root,
            happier_bin=Path("/opt/happier/0.2.17/happier"),
            deadline_utc=NOW_UTC + 1800,
        )
        self.host = FakeHost(self.account)
        self.sessions = FakeSessions(self.host)
        self.repositories = FakeRepositories({"sb-runner": self.account / "repo"})
        self.assignments = FakeAssignments(
            Assignment(
                run_id=run_id,
                principal=PRINCIPAL,
                repository_key="sb-runner",
                base_commit=COMMIT,
                prompt="Do the assigned work.",
                backend="claude",
                model=None,
                permission_mode="default",
                turn_seconds=600,
                deadline_utc=NOW_UTC + 3600,
            )
        )

    def run(self) -> int:
        try:
            return launch(
                self.settings,
                host=self.host,
                assignments=self.assignments,
                repositories=self.repositories,
                sessions=self.sessions,
            )
        except LaunchRefused as refused:
            return refused.exit_code


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_an_authenticated_private_home_runs_one_contained_session_in_a_fresh_clone(rig: Rig) -> None:
    assert rig.run() == EXIT_DONE

    assert rig.host.calls == [
        "status",
        "clone",
        "start_daemon",
        "status",
        "create",
        "list",
        "list",
        "idle",
        "archive",
        "stop_daemon",
    ], rig.host.calls
    assert rig.host.cloned == (rig.account / "repo", rig.settings.workdir, COMMIT)
    assert rig.sessions.created is not None
    assert rig.sessions.created["path"] == rig.settings.workdir
    assert rig.sessions.created["tag"] == f"night:{rig.settings.run_id}"
    assert rig.sessions.created["backend"] == "claude"
    assert rig.sessions.created["terminal_json"] == PLAIN_TERMINAL, (
        "a session must not be handed to a terminal host"
    )
    for directory in (rig.settings.home, rig.settings.runtime_dir):
        assert directory.stat().st_mode & 0o777 == 0o700


def _shared(rig: Rig) -> None:
    rig.settings.happier_home.chmod(0o750)


def _symlinked(rig: Rig) -> None:
    target = rig.settings.happier_home.with_name("real")
    rig.settings.happier_home.rename(target)
    rig.settings.happier_home.symlink_to(target)


def _day_home(rig: Rig) -> None:
    day = rig.account / ".happier"
    day.mkdir(mode=0o700)
    rig.settings = replace(rig.settings, happier_home=day, home=day / "home", runtime_dir=day / "run")


def _inside_account(rig: Rig) -> None:
    work = rig.account / "night-work"
    work.mkdir(mode=0o700)
    rig.settings = replace(rig.settings, workdir_root=work)


def _home_outside(rig: Rig) -> None:
    rig.settings = replace(rig.settings, home=rig.account)


def _workdir_taken(rig: Rig) -> None:
    rig.settings.workdir.mkdir()


def _status(**changes: object) -> Callable[[Rig], None]:
    def change(rig: Rig) -> None:
        rig.host.statuses = [{**IDLE_HOME, **changes}]

    return change


def _other_uid(rig: Rig) -> None:
    rig.settings = replace(rig.settings, uid=rig.settings.uid + 1)


def _untrusted_release(rig: Rig) -> None:
    rig.host.trusted_release = False


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_other_uid, id="wrong-uid"),
        pytest.param(_untrusted_release, id="happier-release-not-root-owned"),
        pytest.param(lambda rig: rig.settings.happier_home.rmdir(), id="home-missing"),
        pytest.param(_shared, id="home-group-readable"),
        pytest.param(_symlinked, id="home-is-a-symlink"),
        pytest.param(_day_home, id="home-is-the-day-home"),
        pytest.param(_inside_account, id="workdir-inside-the-account-home"),
        pytest.param(_home_outside, id="HOME-outside-happier-home"),
        pytest.param(_workdir_taken, id="workdir-already-exists"),
        pytest.param(
            _status(auth={"authenticated": False, "machineRegistered": True}), id="not-authenticated"
        ),
        pytest.param(
            _status(auth={"authenticated": True, "machineRegistered": False}), id="machine-unregistered"
        ),
        pytest.param(_status(daemon={"running": True, "pid": 1}), id="another-daemon-on-the-home"),
        pytest.param(_status(service={"installed": True}), id="a-service-owns-the-home"),
        pytest.param(_status(auth="yes"), id="malformed-status"),
    ],
)
def test_preflight_refuses_before_any_clone_daemon_or_session(
    rig: Rig, change: Callable[[Rig], None]
) -> None:
    change(rig)

    assert rig.run() == EXIT_PREFLIGHT

    assert not {"clone", "start_daemon", "create"} & set(rig.host.calls), rig.host.calls


@pytest.mark.parametrize(
    "assignments",
    [
        pytest.param(FakeAssignments(error=OSError("board unreachable")), id="unreachable"),
        pytest.param(FakeAssignments(error=ValueError("bad shape")), id="malformed"),
    ],
)
def test_no_assignment_means_no_clone_and_no_daemon(rig: Rig, assignments: FakeAssignments) -> None:
    rig.assignments = assignments

    assert rig.run() == EXIT_ASSIGNMENT
    assert rig.host.calls == ["status"]


@pytest.mark.parametrize(
    "field",
    [
        pytest.param({"principal": "pi@dev"}, id="principal"),
        pytest.param({"run_id": str(uuid4())}, id="run"),
        pytest.param({"repository_key": "checkmk"}, id="repository-outside-the-policy"),
    ],
)
def test_an_assignment_for_another_run_principal_or_repository_is_refused(
    rig: Rig, field: dict[str, Any]
) -> None:
    assert rig.assignments.assignment is not None
    rig.assignments.assignment = replace(rig.assignments.assignment, **field)

    assert rig.run() == EXIT_ASSIGNMENT
    assert rig.host.calls == ["status"]


@pytest.mark.parametrize(
    "field",
    [
        pytest.param({"repository_key": "/home/agent/repo"}, id="path-instead-of-key"),
        pytest.param({"repository_key": "https://github.com/x/y"}, id="url-instead-of-key"),
        pytest.param({"base_commit": "main"}, id="branch-not-commit"),
        pytest.param({"backend": "codex"}, id="unreviewed-backend"),
        pytest.param({"model": "x; rm"}, id="model-not-an-id"),
        pytest.param({"permission_mode": "yolo"}, id="yolo"),
        pytest.param({"turn_seconds": 0}, id="no-budget"),
        pytest.param({"turn_seconds": 7200}, id="over-budget"),
        pytest.param({"prompt": ""}, id="empty-prompt"),
        pytest.param({"deadline_utc": float("inf")}, id="infinite-deadline"),
    ],
)
def test_an_assignment_outside_the_contract_cannot_be_constructed(rig: Rig, field: dict[str, Any]) -> None:
    assert rig.assignments.assignment is not None
    with pytest.raises(ValueError):
        replace(rig.assignments.assignment, **field)


def test_the_turn_is_clamped_to_the_bound_run_deadline(rig: Rig) -> None:
    rig.settings = replace(rig.settings, deadline_utc=NOW_UTC + 90)
    rig.sessions.idle_after = 10_000

    assert rig.run() == EXIT_BUDGET
    assert 90 <= rig.host.now < 600


def test_the_turn_is_also_clamped_to_the_assignments_deadline(rig: Rig) -> None:
    assert rig.assignments.assignment is not None
    rig.assignments.assignment = replace(rig.assignments.assignment, deadline_utc=NOW_UTC + 60)
    rig.sessions.idle_after = 10_000

    assert rig.run() == EXIT_BUDGET
    assert 60 <= rig.host.now < 600


def test_a_run_whose_deadline_has_passed_starts_nothing(rig: Rig) -> None:
    rig.settings = replace(rig.settings, deadline_utc=NOW_UTC)

    assert rig.run() == EXIT_BUDGET
    assert rig.host.calls == ["status"]


def test_a_daemon_that_exits_before_ready_fails_and_is_still_stopped(rig: Rig) -> None:
    rig.host.daemon = FakeDaemon(exits_after=0)

    assert rig.run() == EXIT_DAEMON
    assert "create" not in rig.host.calls
    assert rig.host.calls[-1] == "stop_daemon"


def test_a_daemon_that_never_becomes_ready_times_out(rig: Rig) -> None:
    rig.host.statuses = [IDLE_HOME]

    assert rig.run() == EXIT_DAEMON
    assert "create" not in rig.host.calls
    assert rig.host.calls[-1] == "stop_daemon"


def test_a_ready_daemon_that_is_not_the_spawned_process_is_refused(rig: Rig) -> None:
    rig.host.statuses = [IDLE_HOME, _ready(pid=999)]

    assert rig.run() == EXIT_DAEMON
    assert "create" not in rig.host.calls


def test_a_daemon_outside_the_run_cgroup_is_refused(rig: Rig) -> None:
    rig.host.cgroups[DAEMON_PID] = "0::/user.slice/user-1001.slice/user@1001.service/app.slice"

    assert rig.run() == EXIT_DAEMON
    assert "create" not in rig.host.calls
    assert rig.host.calls[-1] == "stop_daemon"


def _escaped(host: FakeHost) -> None:
    host.cgroups[SESSION_PID] = "0::/user.slice/tmux-escape.scope"


def _gone(host: FakeHost) -> None:
    del host.cgroups[SESSION_PID]


def _second_session(host: FakeHost) -> None:
    host.tracked = [[TrackedSession("s-1", SESSION_PID), TrackedSession("s-2", 7)]]


def _never_tracked(host: FakeHost) -> None:
    host.tracked = [[]]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_escaped, id="session-outside-the-run-cgroup"),
        pytest.param(_gone, id="session-process-gone"),
        pytest.param(_second_session, id="a-second-session"),
        pytest.param(_never_tracked, id="never-tracked"),
    ],
)
def test_a_session_not_proven_inside_the_run_cgroup_is_archived_and_refused(
    rig: Rig, change: Callable[[FakeHost], None]
) -> None:
    change(rig.host)

    assert rig.run() == EXIT_SESSION
    assert "idle" not in rig.host.calls
    assert rig.host.calls[-2:] == ["archive", "stop_daemon"]


def test_a_session_still_busy_at_its_turn_budget_is_archived_and_the_daemon_stopped(rig: Rig) -> None:
    rig.sessions.idle_after = 10_000

    assert rig.run() == EXIT_BUDGET
    assert rig.host.calls[-2:] == ["archive", "stop_daemon"]
    assert rig.host.now >= 600


def test_a_failed_session_create_still_stops_the_daemon(rig: Rig) -> None:
    rig.sessions.create_error = HappierError("not_authenticated")

    assert rig.run() == EXIT_SESSION
    assert "archive" not in rig.host.calls
    assert rig.host.calls[-1] == "stop_daemon"


def test_a_daemon_dying_mid_session_is_reported_and_the_session_archived(rig: Rig) -> None:
    rig.sessions.idle_after = 10_000
    rig.host.daemon = FakeDaemon(exits_after=3)

    assert rig.run() == EXIT_DAEMON
    assert rig.host.calls[-2:] == ["archive", "stop_daemon"]


def test_daemon_list_output_is_parsed_strictly() -> None:
    assert tracked_sessions("This daemon has no tracked active sessions. Use `happier session list`.\n") == []
    listed = 'Active sessions:\n[\n  {"happySessionId": "s-1", "pid": 12, "startedBy": "daemon"}\n]\n'
    assert tracked_sessions(listed) == [TrackedSession("s-1", 12)]


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("", id="empty"),
        pytest.param("Sessions:\n[]", id="unknown-header"),
        pytest.param("Active sessions:\nnot json", id="not-json"),
        pytest.param('Active sessions:\n[{"happySessionId": "s-1"}]', id="no-pid"),
        pytest.param('Active sessions:\n[{"happySessionId": "s-1", "pid": true}]', id="boolean-pid"),
        pytest.param('Active sessions:\n{"happySessionId": "s-1", "pid": 3}', id="object-not-list"),
    ],
)
def test_unreadable_daemon_list_output_refuses(output: str) -> None:
    with pytest.raises(LaunchRefused) as refused:
        tracked_sessions(output)

    assert refused.value.exit_code == EXIT_SESSION


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "opt" / "happier" / "0.2.17"
    (release / "package-dist").mkdir(parents=True)
    (release / "package-dist" / "index.mjs").write_text("")
    binary = release / "happier"
    binary.write_text("")
    for path in (tmp_path / "opt", tmp_path / "opt" / "happier", release, release / "package-dist"):
        path.chmod(0o755)
    for path in (binary, release / "package-dist" / "index.mjs"):
        path.chmod(0o755)
    return binary


def _parents_within(path: Path, top: Path) -> tuple[Path, ...]:
    chain = []
    current = path.parent
    while current != top and current != current.parent:
        chain.append(current)
        current = current.parent
    return tuple(chain)


@pytest.fixture
def bounded_parents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "parents", property(lambda self: _parents_within(self, tmp_path)))


@pytest.mark.usefixtures("bounded_parents")
def test_a_release_owned_and_unwritable_throughout_is_trusted(tmp_path: Path) -> None:
    assert release_trusted(_release(tmp_path), owner=os.getuid())


def _writable_module(binary: Path) -> None:
    (binary.parent / "package-dist" / "index.mjs").chmod(0o666)


def _writable_directory(binary: Path) -> None:
    (binary.parent / "package-dist").chmod(0o777)


def _group_writable_binary(binary: Path) -> None:
    binary.chmod(0o775)


def _writable_release_directory(binary: Path) -> None:
    binary.parent.chmod(0o777)


@pytest.mark.usefixtures("bounded_parents")
@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(_writable_module, id="writable-module"),
        pytest.param(_writable_directory, id="writable-directory"),
        pytest.param(_group_writable_binary, id="group-writable-binary"),
        pytest.param(_writable_release_directory, id="writable-release-directory"),
    ],
)
def test_a_writable_entry_anywhere_in_the_release_is_refused(
    tmp_path: Path, spoil: Callable[[Path], None]
) -> None:
    binary = _release(tmp_path)
    spoil(binary)

    assert not release_trusted(binary, owner=os.getuid())


@pytest.mark.usefixtures("bounded_parents")
def test_a_release_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    assert not release_trusted(_release(tmp_path), owner=os.getuid() + 1)


@pytest.mark.usefixtures("bounded_parents")
def test_a_symlinked_binary_is_refused(tmp_path: Path) -> None:
    binary = _release(tmp_path)
    link = binary.with_name("happier-link")
    link.symlink_to(binary)

    assert stat.S_ISLNK(link.lstat().st_mode)
    assert not release_trusted(link, owner=os.getuid())


def _environment(**overrides: str) -> dict[str, str]:
    return {
        PRINCIPAL_ENV: PRINCIPAL,
        "HAPPIER_HOME_DIR": "/var/lib/switchboard-night/happier/claude",
        "HOME": "/var/lib/switchboard-night/happier/claude/home",
        "XDG_RUNTIME_DIR": "/var/lib/switchboard-night/happier/claude/run",
        WORKDIR_ROOT_ENV: "/srv/night/claude",
        HAPPIER_BIN_ENV: "/opt/happier/0.2.17/happier",
        DEADLINE_UTC_ENV: "1000000.5",
        **overrides,
    }


def test_settings_come_only_from_the_exec_steps_environment() -> None:
    run_id = str(uuid4())

    settings = settings_from(run_id, _environment(), 1001)

    assert settings.workdir == Path("/srv/night/claude") / run_id
    assert settings.happier_bin == Path("/opt/happier/0.2.17/happier")
    assert settings.deadline_utc == 1000000.5


def _without(name: str) -> dict[str, str]:
    return {key: value for key, value in _environment().items() if key != name}


@pytest.mark.parametrize(
    ("run_id", "environment", "code"),
    [
        pytest.param("not-a-uuid", _environment(), EXIT_USAGE, id="bad-run-id"),
        pytest.param(str(uuid4()), _without(HAPPIER_BIN_ENV), EXIT_PREFLIGHT, id="no-happier"),
        pytest.param(str(uuid4()), _without(DEADLINE_UTC_ENV), EXIT_PREFLIGHT, id="no-deadline"),
        pytest.param(
            str(uuid4()), _environment(**{DEADLINE_UTC_ENV: "nan"}), EXIT_PREFLIGHT, id="nan-deadline"
        ),
        pytest.param(str(uuid4()), _environment(HOME="relative/home"), EXIT_PREFLIGHT, id="relative-home"),
        pytest.param(
            str(uuid4()), _environment(**{WORKDIR_ROOT_ENV: "/srv/night/../x"}), EXIT_PREFLIGHT, id="dotdot"
        ),
    ],
)
def test_settings_refuse_a_missing_or_unsafe_environment(
    run_id: str, environment: dict[str, str], code: int
) -> None:
    with pytest.raises(LaunchRefused) as refused:
        settings_from(run_id, environment, 1001)

    assert refused.value.exit_code == code


@pytest.mark.parametrize("name", ["home", "runtime_dir"])
def test_a_night_subdirectory_that_aliases_the_day_account_is_refused_before_any_command(
    rig: Rig, name: str
) -> None:
    getattr(rig.settings, name).symlink_to(rig.account, target_is_directory=True)

    with pytest.raises(LaunchRefused) as refused:
        preflight(rig.settings, rig.host)

    assert refused.value.exit_code == EXIT_PREFLIGHT
    assert rig.host.calls == []


@pytest.mark.parametrize("name", ["home", "runtime_dir"])
def test_an_existing_night_subdirectory_must_be_private(rig: Rig, name: str) -> None:
    getattr(rig.settings, name).mkdir(mode=0o755)

    assert rig.run() == EXIT_PREFLIGHT
    assert rig.host.calls == []


def test_night_subdirectories_must_be_the_fixed_names_inside_the_home(rig: Rig) -> None:
    rig.settings = replace(rig.settings, home=rig.settings.happier_home / "elsewhere")

    assert rig.run() == EXIT_PREFLIGHT


def _internal_link(binary: Path) -> None:
    module = binary.parent / "package-dist" / "index.mjs"
    target = binary.parent / "package-dist" / "real.mjs"
    module.rename(target)
    module.symlink_to(target)


@pytest.mark.usefixtures("bounded_parents")
@pytest.mark.parametrize(
    "external_mode",
    [
        pytest.param(0o666, id="to-a-writable-external-file"),
        pytest.param(0o644, id="to-a-protected-external-file"),
    ],
)
def test_any_symlink_inside_the_release_is_refused(tmp_path: Path, external_mode: int) -> None:
    binary = _release(tmp_path)
    external = tmp_path / "external.mjs"
    external.write_text("")
    external.chmod(external_mode)
    module = binary.parent / "package-dist" / "index.mjs"
    module.unlink()
    module.symlink_to(external)

    assert not release_trusted(binary, owner=os.getuid())


@pytest.mark.usefixtures("bounded_parents")
def test_even_an_internal_release_symlink_is_refused(tmp_path: Path) -> None:
    binary = _release(tmp_path)
    _internal_link(binary)

    assert not release_trusted(binary, owner=os.getuid())


def test_the_happier_client_passes_the_plain_terminal_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def run(self: Happier, *args: Any, env: Any = None) -> Any:
        seen.append(tuple(getattr(arg, "text", arg) for arg in args))
        return type(
            "Completed",
            (),
            {"stdout": '{"ok": true, "data": {"session": {"id": "s"}, "created": true}}', "returncode": 0},
        )()

    monkeypatch.setattr(Happier, "_run", run)
    Happier("happier").create(
        backend="claude",
        path=Path("/srv/night/x"),
        tag="t",
        title="t",
        prompt="p",
        permission_mode="default",
        terminal_json=PLAIN_TERMINAL,
    )

    argv = seen[0]
    assert argv[argv.index("--terminal-json") + 1] == PLAIN_TERMINAL


@pytest.mark.parametrize("root", ["happier_home", "workdir_root"])
def test_a_symlinked_ancestor_cannot_redirect_the_private_roots_into_the_day_account(
    rig: Rig, root: str
) -> None:
    night = getattr(rig.settings, root).parent
    destination = rig.account / "aliased-night"
    night.rename(destination)
    night.symlink_to(destination, target_is_directory=True)

    assert rig.run() == EXIT_PREFLIGHT
    assert rig.host.calls == []


def test_an_agent_writable_container_above_the_private_roots_is_refused(rig: Rig) -> None:
    rig.settings.happier_home.parent.chmod(0o777)

    assert rig.run() == EXIT_PREFLIGHT
    assert rig.host.calls == []


def test_the_container_chain_is_checked_up_to_and_including_the_filesystem_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path] = []
    managed = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    world_writable = os.stat_result((stat.S_IFDIR | 0o1777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def lstat(self: Path) -> os.stat_result:
        seen.append(self)
        return world_writable if self == Path("/") and "/" in writable else managed

    writable: set[str] = set()
    monkeypatch.setattr(Path, "lstat", lstat)

    assert trusted_containers(Path("/var/lib/switchboard-night/happier/claude"))
    assert seen == [
        Path("/var/lib/switchboard-night/happier"),
        *Path("/var/lib/switchboard-night/happier").parents,
    ]
    writable.add("/")
    assert not trusted_containers(Path("/var/lib/switchboard-night/happier/claude"))
