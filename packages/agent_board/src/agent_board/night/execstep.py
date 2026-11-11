import logging
import os
import pwd
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NoReturn, Protocol

from agent_board.night.config import VerifierConfig
from agent_board.night.gate import GuardState
from agent_board.night.ledger import Ledger
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import LocalJournal

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

    def drop_to(self, account: Account) -> None:
        """Clears supplementary groups and sets real, effective and saved gid then uid."""
        ...

    def identity(self) -> tuple[tuple[int, int, int], tuple[int, int, int], list[int]]: ...

    def close_inherited_descriptors(self) -> None: ...

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
    account = system.account(agent.uid)
    if account.uid != agent.uid or account.uid <= 0 or account.gid <= 0:
        raise ExecRefused("account_mismatch")
    system.drop_to(Account(account.name, agent.uid, account.gid))
    expected: tuple[tuple[int, int, int], tuple[int, int, int], list[int]] = (
        (agent.uid, agent.uid, agent.uid),
        (account.gid, account.gid, account.gid),
        [],
    )
    if system.identity() != expected:
        raise ExecRefused("drop_unverified")
    system.close_inherited_descriptors()
    python = str(config.launcher_python)
    environment = {
        "HOME": str(agent.home),
        "HAPPIER_HOME_DIR": str(agent.happier_home),
        "XDG_RUNTIME_DIR": f"/run/user/{agent.uid}",
        "USER": account.name,
        "LOGNAME": account.name,
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    system.execve(python, [python, "-I", "-m", config.launcher_module, run_id], environment)


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

    def drop_to(self, account: Account) -> None:
        os.setgroups([])
        os.setresgid(account.gid, account.gid, account.gid)
        os.setresuid(account.uid, account.uid, account.uid)

    def identity(self) -> tuple[tuple[int, int, int], tuple[int, int, int], list[int]]:
        return os.getresuid(), os.getresgid(), os.getgroups()

    def close_inherited_descriptors(self) -> None:
        os.closerange(3, os.sysconf("SC_OPEN_MAX"))

    def execve(self, path: str, argv: list[str], environment: Mapping[str, str]) -> NoReturn:
        os.execve(path, argv, dict(environment))
