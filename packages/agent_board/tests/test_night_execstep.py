import errno
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.config import parse
from agent_board.night.execstep import Account, ExecRefused, exec_native
from agent_board.night.gate import GuardState
from agent_board.night.latch import latch_stop
from agent_board.night.ledger import Arm, Ledger, Pending
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import ClockReading, Grant, LocalJournal

BOOT = str(uuid4())
UID, GID = 1001, 2050
NIGHT_UID, NIGHT_GID = 3001, 3050
FENCE = "f" * 43
OWN = "b" * 32
GUARD = "c" * 32


class Executed(Exception):
    pass


class FakeSystem:
    def __init__(self, run_id: str) -> None:
        self.calls: list[str] = []
        self.cgroup = f"0::{unit_cgroup(f'switchboard-night-{run_id}.service')}"
        self.boot = BOOT
        self.guard_state = GuardState(True, GUARD)
        self.now_boot = 200.0
        self.now_utc = 1_100.0
        self.ids: tuple[tuple[int, int, int], tuple[int, int, int], list[int]] = ((0, 0, 0), (0, 0, 0), [0])
        self.drop_failure: OSError | None = None
        self.skip_drop = False
        self.executed: tuple[str, list[str], Mapping[str, str]] | None = None
        self.looked_up: Account | None = None
        self.supplementary: list[int] = []

    def own_cgroup(self) -> str:
        return self.cgroup

    def boot_id(self) -> str:
        return self.boot

    def guard(self, unit: str) -> GuardState:
        return self.guard_state

    def boottime(self) -> float:
        return self.now_boot

    def utc(self) -> float:
        return self.now_utc

    def account(self, uid: int) -> Account:
        return self.looked_up or Account("agent-night", uid, NIGHT_GID if uid == NIGHT_UID else GID)

    def supplementary_groups(self, account: Account) -> list[int]:
        return list(self.supplementary)

    def drop_to(self, account: Account) -> None:
        self.calls.append("drop")
        if self.drop_failure is not None:
            raise self.drop_failure
        if not self.skip_drop:
            self.ids = ((account.uid,) * 3, (account.gid,) * 3, [])

    def identity(self) -> tuple[tuple[int, int, int], tuple[int, int, int], list[int]]:
        return self.ids

    def close_inherited_descriptors(self) -> None:
        self.calls.append("close")

    def execve(self, path: str, argv: list[str], environment: Mapping[str, str]) -> NoReturn:
        self.calls.append("execve")
        self.executed = (path, argv, dict(environment))
        raise Executed


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


def _config() -> Any:
    return parse(
        {
            "box": "huginn",
            "principal": "verifier@huginn",
            "namespace": "switchboard",
            "board_url": "https://board.test.invalid",
            "signer_key": "/etc/switchboard-night/verifier_ed25519",
            "state_path": "/var/lib/switchboard-night/journal.sqlite3",
            "slice": "switchboard-night.slice",
            "launcher": {
                "python": "/opt/switchboard-night/releases/abc/bin/python",
                "module": "agent_board.night.launch",
                "happier": "/opt/happier/0.2.17/happier",
            },
            "agents": {
                "pi@huginn": {
                    "uid": UID,
                    "night_uid": NIGHT_UID,
                    "night_gid": NIGHT_GID,
                    "home": "/home/agent",
                    "happier_home": "/var/lib/switchboard-night/happier/pi",
                    "workdir_root": "/srv/night/pi",
                }
            },
            "limits": {"memory_max": "600M", "tasks_max": 128, "cpu_quota": "100%", "stop_grace_seconds": 5},
        }
    )


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.journal = LocalJournal(
            tmp_path / "state" / "journal.sqlite3",
            box="huginn",
            principals={"pi@huginn": UID},
            clock=lambda: ClockReading(BOOT, 150.0, 1_050.0),
        )
        self.ledger = Ledger(tmp_path / "state" / "ledger.sqlite3")
        self.run_id = str(uuid4())
        self.journal.reserve(Grant(self.run_id, FENCE, "pi@huginn", UID, BOOT, 700.0, 1_600.0))
        self.journal.claim_start(self.run_id, FENCE)
        self.ledger.record_binding(self.run_id, uid=UID, boot_id=BOOT, invocation_id=OWN, cgroup="/x")
        receipt = Pending(str(uuid4()), "binding", self.run_id, {"receipt_id": "r"})
        self.ledger.persist(receipt)
        self.ledger.acknowledge(receipt.receipt_id, journal_id=str(uuid4()), counter=1)
        self.ledger.arm(Arm(self.run_id, BOOT, GUARD, 700.0, 1_600.0))
        self.system = FakeSystem(self.run_id)

    def run(self, invocation: str = OWN) -> None:
        exec_native(
            self.run_id,
            config=_config(),
            journal=self.journal,
            ledger=self.ledger,
            environ={"INVOCATION_ID": invocation, "SECRET": "root-only"},
            system=self.system,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_bound_armed_live_run_drops_to_the_agent_then_execs_the_launcher(rig: Rig) -> None:
    with pytest.raises(Executed):
        rig.run()

    assert rig.system.calls == ["drop", "close", "execve"], rig.system.calls
    assert rig.system.executed is not None
    path, argv, environment = rig.system.executed
    assert path == "/opt/switchboard-night/releases/abc/bin/python"
    assert argv == [path, "-I", "-m", "agent_board.night.launch", rig.run_id]
    assert environment["HOME"] == "/var/lib/switchboard-night/happier/pi/home"
    assert environment["XDG_RUNTIME_DIR"] == "/var/lib/switchboard-night/happier/pi/run"
    assert environment["HAPPIER_HOME_DIR"] == "/var/lib/switchboard-night/happier/pi"
    assert environment["SWITCHBOARD_NIGHT_PRINCIPAL"] == "pi@huginn"
    assert environment["SWITCHBOARD_NIGHT_WORKDIR_ROOT"] == "/srv/night/pi"
    assert environment["SWITCHBOARD_NIGHT_HAPPIER"] == "/opt/happier/0.2.17/happier"
    assert float(environment["SWITCHBOARD_NIGHT_DEADLINE_UTC"]) == 1_600.0 - 5
    assert "SECRET" not in environment and "INVOCATION_ID" not in environment, "root environment leaked"


def test_an_exec_that_starts_after_the_stop_commit_refuses_even_without_any_freeze(rig: Rig) -> None:
    rig.journal.request_stop(rig.run_id, FENCE)

    with pytest.raises(ExecRefused, match="not_runnable"):
        rig.run()

    assert rig.system.calls == [], "the late exec reached the drop or exec"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        pytest.param(
            lambda system: setattr(system, "cgroup", "0::/user.slice/x"),
            "outside_unit_cgroup",
            id="other-cgroup",
        ),
        pytest.param(lambda system: setattr(system, "boot", str(uuid4())), "not_runnable", id="later-boot"),
        pytest.param(
            lambda system: setattr(system, "guard_state", GuardState(True, "d" * 32)),
            "not_armed",
            id="other-guard",
        ),
        pytest.param(
            lambda system: setattr(system, "guard_state", GuardState(False, GUARD)),
            "not_armed",
            id="guard-down",
        ),
        pytest.param(lambda system: setattr(system, "now_boot", 695.0), "deadline", id="boot-deadline-grace"),
        pytest.param(lambda system: setattr(system, "now_utc", 1_595.0), "deadline", id="utc-deadline-grace"),
    ],
)
def test_the_exec_refuses_before_any_drop_when_a_runtime_check_fails(
    rig: Rig, change: Any, reason: str
) -> None:
    change(rig.system)

    with pytest.raises(ExecRefused, match=reason):
        rig.run()

    assert rig.system.calls == []


def test_a_stale_invocation_cannot_exec(rig: Rig) -> None:
    with pytest.raises(ExecRefused, match="binding_mismatch"):
        rig.run(invocation="e" * 32)

    assert rig.system.calls == []


def test_the_drop_uses_the_accounts_primary_gid_and_clears_supplementary_groups(rig: Rig) -> None:
    with pytest.raises(Executed):
        rig.run()

    assert rig.system.ids == ((NIGHT_UID,) * 3, (NIGHT_GID,) * 3, []), rig.system.ids


def test_a_failed_drop_never_reaches_the_launcher(rig: Rig) -> None:
    rig.system.drop_failure = PermissionError("setresuid refused")

    with pytest.raises(PermissionError):
        rig.run()

    assert "execve" not in rig.system.calls


def test_an_unverified_drop_never_reaches_the_launcher(rig: Rig) -> None:
    rig.system.skip_drop = True

    with pytest.raises(ExecRefused, match="drop_unverified"):
        rig.run()

    assert rig.system.calls == ["drop"], rig.system.calls


@pytest.mark.parametrize(
    "looked_up",
    [
        pytest.param(Account("root", 0, 0), id="lookup-returns-root"),
        pytest.param(Account("other", 1002, NIGHT_GID), id="lookup-returns-another-principal"),
        pytest.param(Account("agent-night", NIGHT_UID, 0), id="lookup-returns-root-group"),
        pytest.param(Account("agent", UID, GID), id="lookup-returns-the-requesting-day-account"),
    ],
)
def test_an_account_lookup_that_disagrees_with_the_configured_uid_is_refused_before_dropping(
    rig: Rig, looked_up: Account
) -> None:
    rig.system.looked_up = looked_up

    with pytest.raises(ExecRefused, match="account_mismatch"):
        rig.run()

    assert rig.system.calls == [], "a substituted identity reached the drop"


class StoppedManager:
    def show(self, unit: str, properties: object) -> str:
        return "LoadState=loaded\nActiveState=inactive\nJob=\nControlGroup=\n"


def test_a_late_exec_after_latch_stop_refuses_before_any_drop_or_exec(rig: Rig) -> None:
    def absent(path: Path) -> str:
        raise FileNotFoundError(errno.ENOENT, "absent", str(path))

    outcome = latch_stop(
        _config(), lambda: rig.journal, StoppedManager(), run_id=rig.run_id, fence=FENCE, events=absent
    )

    assert outcome.latched == [rig.run_id], outcome
    with pytest.raises(ExecRefused, match="not_runnable"):
        rig.run()
    assert rig.system.calls == [], "the late exec reached the uid drop or native exec after the latch"


def test_the_launcher_learns_both_the_night_and_the_requesting_uid(rig: Rig) -> None:
    with pytest.raises(Executed):
        rig.run()

    assert rig.system.executed is not None
    environment = rig.system.executed[2]
    assert environment["SWITCHBOARD_NIGHT_UID"] == str(NIGHT_UID)
    assert environment["SWITCHBOARD_NIGHT_DAY_UID"] == str(UID)
    assert environment["USER"] == "agent-night"


@pytest.mark.parametrize(
    ("looked_up", "supplementary"),
    [
        pytest.param(Account("agent-night", NIGHT_UID, GID), [], id="primary-gid-is-not-the-dedicated-one"),
        pytest.param(Account("agent-night", NIGHT_UID, NIGHT_GID), [27], id="member-of-sudo"),
        pytest.param(Account("agent-night", NIGHT_UID, NIGHT_GID), [GID], id="member-of-the-day-group"),
    ],
)
def test_a_night_account_that_is_not_dedicated_is_refused_before_dropping(
    rig: Rig, looked_up: Account, supplementary: list[int]
) -> None:
    rig.system.looked_up = looked_up
    rig.system.supplementary = supplementary

    with pytest.raises(ExecRefused, match="night_account_not_dedicated"):
        rig.run()

    assert rig.system.calls == []
