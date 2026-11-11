import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from agent_board.night.config import VerifierConfig
from agent_board.night.controller import Observation, RunManagerError, RunSpec
from agent_board.night.ledger import Ledger
from agent_board.night_inspection import (
    CGROUP_ROOT,
    NIGHT_SLICE,
    Binding,
    EmptyObservation,
    InspectionError,
    unit_cgroup,
)

GATE_TIMEOUT_SECONDS = 30
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C"}
_COMMAND_TIMEOUT_SECONDS = 15
logger = logging.getLogger(__name__)


class StopInspector(Protocol):
    def stopped(self, binding: Binding) -> EmptyObservation: ...


class Commands(Protocol):
    def run(self, argv: Sequence[str]) -> str: ...

    def kill_cgroup(self, cgroup: Path) -> bool: ...


class LiveCommands:
    def run(self, argv: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                list(argv),
                env=_ENVIRONMENT,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RunManagerError(f"{argv[0]} timed out") from None
        if completed.returncode != 0:
            raise RunManagerError(
                f"{argv[0]} exited {completed.returncode}: {completed.stderr.strip()[:200]}"
            )
        return completed.stdout

    def kill_cgroup(self, cgroup: Path) -> bool:
        try:
            (cgroup / "cgroup.kill").write_text("1")
        except FileNotFoundError:
            return False
        return True


def run_argv(config: VerifierConfig, config_path: Path, spec: RunSpec) -> tuple[str, ...]:
    """The one transient unit a run may be: bound to the guard, gated by root, then dropped to the agent."""
    agent, limits, python = spec.agent, config.limits, str(config.launcher_python)
    if any(_unsafe_in_command_line(text) for text in (python, str(config_path))):
        raise RunManagerError("the launcher python or config path cannot appear in a systemd command line")
    gate = f"+{python} -I -m agent_board.night gate --config {config_path} {spec.run_id}"
    return (
        SYSTEMD_RUN,
        "--system",
        "--no-block",
        "--quiet",
        f"--unit={spec.unit}",
        f"--slice={NIGHT_SLICE}",
        f"--uid={agent.uid}",
        "--property=Type=exec",
        "--property=Delegate=no",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=Restart=no",
        "--property=NoNewPrivileges=yes",
        "--property=UMask=0077",
        f"--property=BindsTo={config.guard_unit}",
        f"--property=After={config.guard_unit}",
        f"--property=TimeoutStartSec={GATE_TIMEOUT_SECONDS}",
        f"--property=TimeoutStopSec={limits.stop_grace_seconds}",
        f"--property=RuntimeMaxSec={max(1, int(spec.runtime_max_seconds))}",
        f"--property=MemoryMax={limits.memory_max}",
        f"--property=TasksMax={limits.tasks_max}",
        f"--property=CPUQuota={limits.cpu_quota}",
        f"--property=ExecStartPre={gate}",
        f"--setenv=HOME={agent.home}",
        f"--setenv=HAPPIER_HOME_DIR={agent.happier_home}",
        f"--setenv=XDG_RUNTIME_DIR=/run/user/{agent.uid}",
        f"--working-directory={agent.workdir_root}",
        "--",
        python,
        "-I",
        "-m",
        config.launcher_module,
        spec.run_id,
    )


def _unsafe_in_command_line(text: str) -> bool:
    return any(character.isspace() or character in "\"'\\$%;" for character in text)


class SystemdRuns:
    """Starts, stops and inspects night runs through fixed systemd argv and the root-owned cgroup only."""

    def __init__(
        self,
        config: VerifierConfig,
        config_path: Path,
        ledger: Ledger,
        inspector: StopInspector,
        commands: Commands,
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._ledger = ledger
        self._inspector = inspector
        self._commands = commands

    def launch(self, spec: RunSpec) -> None:
        self._commands.run(run_argv(self._config, self._config_path, spec))

    def stop(self, unit: str) -> None:
        try:
            self._commands.run((SYSTEMCTL, "--system", "stop", "--no-block", unit))
        finally:
            if self._commands.kill_cgroup(CGROUP_ROOT / unit_cgroup(unit).lstrip("/")):
                logger.info("killed the cgroup of %s", unit)

    def inspect(self, unit: str) -> Observation:
        binding = self._binding(unit)
        if binding is None:
            return Observation(unit_active=True, cgroup_populated=None)
        try:
            self._inspector.stopped(binding)
        except InspectionError as error:
            logger.warning("%s is not proven stopped: %s", unit, error)
            return Observation(unit_active=True, cgroup_populated=None)
        return Observation(unit_active=False, cgroup_populated=False)

    def _binding(self, unit: str) -> Binding | None:
        run_id = unit.removeprefix("switchboard-night-").removesuffix(".service")
        recorded = self._ledger.binding(run_id)
        if recorded is None:
            return None
        return Binding(run_id, recorded.uid, recorded.boot_id, recorded.invocation_id)
