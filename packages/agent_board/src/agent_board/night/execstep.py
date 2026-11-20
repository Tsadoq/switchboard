import logging
import os
import pwd
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NoReturn, Protocol

from agent_board.night.assignment import AssignmentError, decode, seal
from agent_board.night.config import EXISTING_LOGIN, VerifierConfig
from agent_board.night.environment import (
    ASSIGNMENT_FD_ENV,
    CLI_ENV,
    DAY_UID_ENV,
    DEADLINE_UTC_ENV,
    HAPPIER_BIN_ENV,
    HOME_DIR_ENV,
    HOME_NAME,
    LOGIN_ENV,
    NIGHT_UID_ENV,
    PRINCIPAL_ENV,
    RUNTIME_DIR_NAME,
    TOKEN_FILE_ENV,
    WORKDIR_ROOT_ENV,
)
from agent_board.night.gate import GuardState
from agent_board.night.ledger import CapturedAssignment, Ledger
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import LocalJournal

CLI_LAUNCHER = "agent_board.night.cli_launch"

_INVOCATION = re.compile(r"[0-9a-f]{32}")
logger = logging.getLogger(__name__)


class ExecRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int


class System(Protocol):
    """The operating-system calls the exec step makes, so every check is observable in tests."""

    def own_cgroup(self) -> str: ...

    def boot_id(self) -> str: ...

    def guard(self, unit: str) -> GuardState: ...

    def boottime(self) -> float: ...

    def utc(self) -> float: ...

    def account(self, uid: int) -> Account: ...

    def supplementary_groups(self, account: Account) -> list[int]:
        """The account's groups besides its primary one, as the group database lists them."""
        ...

    def drop_to(self, account: Account) -> None:
        """Clears supplementary groups and sets real, effective and saved gid then uid."""
        ...

    def identity(self) -> tuple[tuple[int, int, int], tuple[int, int, int], list[int]]: ...

    def close_inherited_descriptors(self, keep: int) -> None: ...

    def execve(self, path: str, argv: list[str], environment: Mapping[str, str]) -> NoReturn: ...


def exec_native(
    run_id: str,
    *,
    config: VerifierConfig,
    journal: LocalJournal,
    ledger: Ledger,
    environ: Mapping[str, str],
    system: System,
) -> NoReturn:
    """The unit's main process: re-checks everything as root at the last moment, drops to the agent, then execs.

    Every stop path commits request_stop to the journal before its first kill, so this step either started
    before that commit, and is in the cgroup the kill empties, or reads the stop and refuses.
    """
    unit = f"switchboard-night-{run_id}.service"
    own = environ.get("INVOCATION_ID", "")
    if _INVOCATION.fullmatch(own) is None:
        raise ExecRefused("no_invocation")
    if system.own_cgroup() != f"0::{unit_cgroup(unit)}":
        raise ExecRefused("outside_unit_cgroup")
    boot = system.boot_id()
    record = next((each for each in journal.active() if each.run_id == run_id), None)
    if record is None or record.state != "starting" or record.stop_requested or record.uncertain:
        raise ExecRefused("not_runnable")
    if not record.launch_claimed or record.boot_id != boot:
        raise ExecRefused("not_runnable")
    binding = ledger.binding(run_id)
    if (
        binding is None
        or not binding.server_acked
        or binding.invocation_id != own
        or binding.boot_id != boot
        or binding.uid != record.uid
    ):
        raise ExecRefused("binding_mismatch")
    guard = system.guard(config.guard_unit)
    if not guard.active or not ledger.ready(
        run_id,
        boot_id=boot,
        guard_invocation_id=guard.invocation_id,
        deadline_boottime=record.deadline_boottime,
        deadline_utc=record.deadline_utc,
    ):
        raise ExecRefused("not_armed")
    grace = config.limits.stop_grace_seconds
    if system.boottime() >= record.deadline_boottime - grace or system.utc() >= record.deadline_utc - grace:
        raise ExecRefused("deadline")
    agent = config.agents.get(record.principal)
    if agent is None or agent.uid != record.uid:
        raise ExecRefused("principal_mismatch")
    assignment = _captured_assignment(ledger, run_id, record.principal)
    exec_uid = agent.exec_uid
    account = system.account(exec_uid)
    if account.uid != exec_uid or account.uid <= 0 or account.gid <= 0:
        raise ExecRefused("account_mismatch")
    if agent.night_gid is not None and (
        account.gid != agent.night_gid
        or system.supplementary_groups(account) != []
        or agent.night_gid in _requesting_groups(config, system)
    ):
        raise ExecRefused("night_account_not_dedicated")
    system.drop_to(Account(account.name, exec_uid, account.gid))
    expected: tuple[tuple[int, int, int], tuple[int, int, int], list[int]] = (
        (exec_uid, exec_uid, exec_uid),
        (account.gid, account.gid, account.gid),
        [],
    )
    if system.identity() != expected:
        raise ExecRefused("drop_unverified")
    try:
        assignment_fd = seal(assignment)
    except AssignmentError as error:
        raise ExecRefused(f"assignment_unsealed:{error}") from None
    system.close_inherited_descriptors(keep=assignment_fd)
    python = str(config.launcher_python)
    environment = {
        "USER": account.name,
        "LOGNAME": account.name,
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        PRINCIPAL_ENV: record.principal,
        WORKDIR_ROOT_ENV: str(agent.workdir_root),
        NIGHT_UID_ENV: str(exec_uid),
        DAY_UID_ENV: str(record.uid),
        DEADLINE_UTC_ENV: repr(record.deadline_utc - grace),
        ASSIGNMENT_FD_ENV: str(assignment_fd),
    }
    if agent.existing_login and agent.cli is not None:
        environment |= {"HOME": str(agent.home), LOGIN_ENV: EXISTING_LOGIN, CLI_ENV: str(agent.cli)}
        if agent.token_file is not None:
            environment[TOKEN_FILE_ENV] = str(agent.token_file)
    elif agent.happier_home is not None:
        environment |= {
            "HOME": str(agent.happier_home / HOME_NAME),
            HOME_DIR_ENV: str(agent.happier_home),
            "XDG_RUNTIME_DIR": str(agent.happier_home / RUNTIME_DIR_NAME),
        }
        if config.launcher_happier is not None:
            environment[HAPPIER_BIN_ENV] = str(config.launcher_happier)
    else:
        raise ExecRefused("agent_login_unconfigured")
    module = CLI_LAUNCHER if agent.existing_login else config.launcher_module
    system.execve(python, [python, "-I", "-m", module, run_id], environment)


def _captured_assignment(ledger: Ledger, run_id: str, principal: str) -> bytes:
    captured = ledger.captured(run_id)
    if not isinstance(captured, CapturedAssignment) or captured.aborted_after_apply:
        raise ExecRefused("no_captured_assignment")
    if captured.assignment is None:
        raise ExecRefused("empty_assignment")
    try:
        decoded = decode(captured.assignment)
    except AssignmentError:
        raise ExecRefused("invalid_assignment") from None
    if decoded.run_id != run_id or decoded.principal != principal:
        raise ExecRefused("assignment_mismatch")
    return captured.assignment


def _requesting_groups(config: VerifierConfig, system: System) -> set[int]:
    """Every primary and supplementary group of every configured requesting account."""
    groups: set[int] = set()
    for uid in {agent.uid for agent in config.agents.values()}:
        try:
            day = system.account(uid)
        except KeyError:
            raise ExecRefused("requesting_account_unknown") from None
        groups.add(day.gid)
        groups.update(system.supplementary_groups(day))
    return groups


class LiveSystem:
    def __init__(self, guard_state: Callable[[str], GuardState], boot_id: Callable[[], str]) -> None:
        self._guard_state = guard_state
        self._boot_id = boot_id

    def own_cgroup(self) -> str:
        with open("/proc/self/cgroup") as stream:
            return stream.read().strip()

    def boot_id(self) -> str:
        return self._boot_id()

    def guard(self, unit: str) -> GuardState:
        return self._guard_state(unit)

    def boottime(self) -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME)

    def utc(self) -> float:
        return time.time()

    def account(self, uid: int) -> Account:
        entry = pwd.getpwuid(uid)
        return Account(entry.pw_name, entry.pw_uid, entry.pw_gid)

    def supplementary_groups(self, account: Account) -> list[int]:
        return sorted(set(os.getgrouplist(account.name, account.gid)) - {account.gid})

    def drop_to(self, account: Account) -> None:
        os.setgroups([])
        os.setresgid(account.gid, account.gid, account.gid)
        os.setresuid(account.uid, account.uid, account.uid)

    def identity(self) -> tuple[tuple[int, int, int], tuple[int, int, int], list[int]]:
        return os.getresuid(), os.getresgid(), os.getgroups()

    def close_inherited_descriptors(self, keep: int) -> None:
        os.closerange(3, keep)
        os.closerange(keep + 1, os.sysconf("SC_OPEN_MAX"))
        os.set_inheritable(keep, True)

    def execve(self, path: str, argv: list[str], environment: Mapping[str, str]) -> NoReturn:
        os.execve(path, argv, dict(environment))
