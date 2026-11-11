import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.config import PRODUCTION_GUARD_UNIT, VerifierConfigError, parse
from agent_board.night.controller import RunManagerError, RunSpec, unit_name
from agent_board.night.gate import GuardState, open_gate
from agent_board.night.ledger import Arm, Ledger, Pending
from agent_board.night.systemd import SystemdRuns, run_argv
from agent_board.night_inspection import Binding, EmptyObservation, InspectionError
from agent_board.night_state import ClockReading, Grant, LocalJournal

BOOT = str(uuid4())
UID = 1001
FENCE = "f" * 43
OWN = "b" * 32
GUARD = "c" * 32
CONFIG_PATH = Path("/etc/switchboard-night/verifier.toml")
SERVICE_PYTHON = Path("/opt/switchboard-night/releases/abc/bin/python")


def _config(**changes: Any) -> Any:
    table: dict[str, Any] = {
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
        },
        "agents": {
            "pi@huginn": {
                "uid": UID,
                "home": "/home/agent",
                "happier_home": "/var/lib/switchboard-night/happier/pi",
                "workdir_root": "/srv/night/pi",
            }
        },
        "limits": {"memory_max": "600M", "tasks_max": 128, "cpu_quota": "100%", "stop_grace_seconds": 5},
    }
    table.update(changes)
    return parse(table)


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


def _spec(config: Any, run_id: str, runtime: float = 600.0) -> RunSpec:
    return RunSpec(run_id, unit_name(run_id), config.agents["pi@huginn"], runtime)


def test_the_run_unit_is_bound_to_the_guard_gated_by_root_and_dropped_to_the_agent() -> None:
    config = _config()
    run_id = str(uuid4())

    argv = run_argv(config, CONFIG_PATH, _spec(config, run_id, 594.7), SERVICE_PYTHON)
    options, command = argv[: argv.index("--")], argv[argv.index("--") + 1 :]

    for expected in (
        "--system",
        f"--unit=switchboard-night-{run_id}.service",
        "--slice=switchboard-night.slice",
        f"--uid={UID}",
        "--property=Delegate=no",
        "--property=KillMode=control-group",
        "--property=Restart=no",
        f"--property=BindsTo={PRODUCTION_GUARD_UNIT}",
        f"--property=After={PRODUCTION_GUARD_UNIT}",
        "--property=RuntimeMaxSec=594",
        "--setenv=HAPPIER_HOME_DIR=/var/lib/switchboard-night/happier/pi",
    ):
        assert expected in options, f"missing {expected}: {options}"
    gate = next(option for option in options if option.startswith("--property=ExecStartPre="))
    assert gate == (
        "--property=ExecStartPre=+/opt/switchboard-night/releases/abc/bin/python -I -m agent_board.night gate "
        f"--config {CONFIG_PATH} {run_id}"
    ), gate
    assert command == (
        "/opt/switchboard-night/releases/abc/bin/python",
        "-I",
        "-m",
        "agent_board.night.launch",
        run_id,
    )


def test_a_config_path_that_would_split_the_gate_command_is_refused() -> None:
    config = _config()

    with pytest.raises(RunManagerError):
        run_argv(config, Path("/etc/night verifier.toml"), _spec(config, str(uuid4())), SERVICE_PYTHON)


class Commands:
    def __init__(self) -> None:
        self.ran: list[tuple[str, ...]] = []
        self.killed: list[Path] = []

    def run(self, argv: Sequence[str]) -> str:
        self.ran.append(tuple(argv))
        return ""

    def kill_cgroup(self, cgroup: Path) -> bool:
        self.killed.append(cgroup)
        return True


class Inspector:
    def __init__(self) -> None:
        self.failure: InspectionError | None = None
        self.checked: list[Binding] = []

    def stopped(self, binding: Binding) -> EmptyObservation:
        self.checked.append(binding)
        if self.failure is not None:
            raise self.failure
        return EmptyObservation(binding, cgroup_present=False, unit_loaded=False)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "state" / "ledger.sqlite3")


def _runs(ledger: Ledger, inspector: Inspector, commands: Commands) -> SystemdRuns:
    return SystemdRuns(_config(), CONFIG_PATH, ledger, inspector, commands, SERVICE_PYTHON)


def test_stop_asks_systemd_then_kills_the_root_owned_cgroup(ledger: Ledger) -> None:
    commands = Commands()
    unit = unit_name(str(uuid4()))

    _runs(ledger, Inspector(), commands).stop(unit)

    assert commands.ran == [("/usr/bin/systemctl", "--system", "stop", "--no-block", unit)], commands.ran
    assert commands.killed == [Path("/sys/fs/cgroup/switchboard.slice/switchboard-night.slice") / unit], (
        commands.killed
    )


def test_stop_proof_comes_from_the_inspector_with_the_recorded_binding(ledger: Ledger) -> None:
    run_id = str(uuid4())
    ledger.record_binding(run_id, uid=UID, boot_id=BOOT, invocation_id=OWN, cgroup="/x")
    inspector = Inspector()
    runs = _runs(ledger, inspector, Commands())

    proven = runs.inspect(unit_name(run_id))
    inspector.failure = InspectionError("populated_or_unknown")
    unproven = runs.inspect(unit_name(run_id))
    unknown = runs.inspect(unit_name(str(uuid4())))

    assert inspector.checked[0] == Binding(run_id, UID, BOOT, OWN), inspector.checked
    assert (proven.unit_active, proven.cgroup_populated) == (False, False)
    assert unproven.cgroup_populated is None and unknown.cgroup_populated is None


class GateRig:
    def __init__(self, tmp_path: Path) -> None:
        self.journal = LocalJournal(
            tmp_path / "state" / "journal.sqlite3",
            box="huginn",
            principals={"pi@huginn": UID},
            clock=lambda: ClockReading(BOOT, 100.0, 1_000.0),
        )
        self.ledger = Ledger(tmp_path / "state" / "ledger.sqlite3")
        self.run_id = str(uuid4())
        self.journal.reserve(Grant(self.run_id, FENCE, "pi@huginn", UID, BOOT, 700.0, 1_600.0))
        self.journal.claim_start(self.run_id, FENCE)
        self.guard = GuardState(True, GUARD)
        self.clock = 0.0
        self.sleeps = 0

    def bind_and_arm(self, guard: str = GUARD, invocation: str = OWN) -> None:
        self.ledger.record_binding(self.run_id, uid=UID, boot_id=BOOT, invocation_id=invocation, cgroup="/x")
        receipt = Pending(str(uuid4()), "binding", self.run_id, {"receipt_id": "r"})
        self.ledger.persist(receipt)
        self.ledger.acknowledge(receipt.receipt_id, journal_id=str(uuid4()), counter=1)
        self.ledger.arm(Arm(self.run_id, BOOT, guard, 700.0, 1_600.0))

    def open(self, own: str = OWN) -> bool:
        def sleep(seconds: float) -> None:
            self.sleeps += 1
            self.clock += seconds

        return open_gate(
            self.run_id,
            ledger=self.ledger,
            journal=self.journal,
            environ={"INVOCATION_ID": own},
            boot_id=lambda: BOOT,
            guard=lambda: self.guard,
            boottime=lambda: self.clock,
            sleep=sleep,
        )


def test_the_gate_opens_only_for_its_own_bound_and_armed_unit(tmp_path: Path) -> None:
    rig = GateRig(tmp_path)
    rig.bind_and_arm()

    assert rig.open()
    assert not rig.open(own="d" * 32), "the gate opened for a different unit invocation"


def test_the_gate_times_out_closed_without_the_boards_binding(tmp_path: Path) -> None:
    rig = GateRig(tmp_path)

    assert not rig.open()
    assert rig.clock >= 20, f"gave up too early: {rig.clock}"


def test_an_arm_from_an_earlier_guard_or_an_inactive_guard_keeps_the_gate_shut(tmp_path: Path) -> None:
    rig = GateRig(tmp_path)
    rig.bind_and_arm(guard="e" * 32)
    stale = rig.open()
    rig.guard = GuardState(False, "e" * 32)

    assert not stale, "an arm from an earlier guard invocation opened the gate"
    assert not rig.open(), "an inactive guard opened the gate"


def test_a_smoke_guard_name_is_only_accepted_with_the_acceptance_stub() -> None:
    stub = {
        "python": "/opt/switchboard-night/releases/abc/bin/python",
        "module": "agent_board.night.acceptance_stub",
    }

    smoke = _config(guard_unit="switchboard-night-guard-smoke.service", launcher=stub)

    assert smoke.guard_unit == "switchboard-night-guard-smoke.service"
    with pytest.raises(VerifierConfigError, match="acceptance stub"):
        _config(guard_unit="switchboard-night-guard-smoke.service")
    with pytest.raises(VerifierConfigError, match="guard_unit"):
        _config(guard_unit="getty@tty1.service", launcher=stub)
    assert _config().guard_unit == PRODUCTION_GUARD_UNIT


def test_the_run_binds_to_the_configured_guard() -> None:
    stub = {
        "python": "/opt/switchboard-night/releases/abc/bin/python",
        "module": "agent_board.night.acceptance_stub",
    }
    config = _config(guard_unit="switchboard-night-guard-smoke.service", launcher=stub)

    argv = run_argv(config, CONFIG_PATH, _spec(config, str(uuid4())), SERVICE_PYTHON)

    assert "--property=BindsTo=switchboard-night-guard-smoke.service" in argv
    assert "--property=After=switchboard-night-guard-smoke.service" in argv
    assert not any(PRODUCTION_GUARD_UNIT in option for option in argv), argv


def test_the_root_gate_runs_the_service_interpreter_never_the_configured_launcher() -> None:
    agent_venv = {"python": "/srv/night/pi/venv/bin/python", "module": "agent_board.night.launch"}
    config = _config(launcher=agent_venv)
    run_id = str(uuid4())

    argv = run_argv(config, CONFIG_PATH, _spec(config, run_id), SERVICE_PYTHON)

    gate = next(option for option in argv if option.startswith("--property=ExecStartPre="))
    assert gate.startswith(f"--property=ExecStartPre=+{SERVICE_PYTHON} -I -m agent_board.night gate"), gate
    assert "/srv/night/pi/venv" not in gate, "a configured launcher interpreter reached the root gate"
    assert argv[argv.index("--") + 1] == "/srv/night/pi/venv/bin/python", (
        "the launcher itself still runs as the agent"
    )
