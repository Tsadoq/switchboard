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
    ROOT_EXEC,
    Binding,
    EmptyObservation,
    InspectionError,
    unit_cgroup,
)

GATE_TIMEOUT_SECONDS = 30
DROP_CAPABILITIES = ("CAP_SETUID", "CAP_SETGID", "CAP_SETPCAP")
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C"}
_COMMAND_TIMEOUT_SECONDS = 15
logger = logging.getLogger(__name__)


class StopInspector(Protocol):
    def stopped(self, binding: Binding) -> EmptyObservation: ...


class Commands(Protocol):
    def run(self, argv: Sequence[str]) -> str: ...

    def freeze_cgroup(self, cgroup: Path) -> bool: ...

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

    def freeze_cgroup(self, cgroup: Path) -> bool:
        return _write_control(cgroup / "cgroup.freeze")

    def kill_cgroup(self, cgroup: Path) -> bool:
        return _write_control(cgroup / "cgroup.kill")


def _write_control(control: Path) -> bool:
    try:
        control.write_text("1")
    except FileNotFoundError:
        return False
    return True


def run_argv(
    config: VerifierConfig, config_path: Path, spec: RunSpec, service_python: Path
) -> tuple[str, ...]:
    """The one transient unit a run may be: bound to the guard, root-gated and root-exec'd, hardened throughout.

    The unit runs as root (a transient unit cannot give a root main process while carrying User=), so the
    pinned service interpreter runs both the gate and the exec step, which drops to the agent before any
    native code. The unit properties bound what that root could do even if the drop were skipped.
    """
    agent, limits = spec.agent, config.limits
    paths = (
        service_python,
        config_path,
        agent.workdir_root,
        agent.happier_home,
        config.state_path.parent,
        config.signer_key,
    )
    if any(_unsafe_in_command_line(str(path)) for path in paths):
        raise RunManagerError("a path in the night unit's command line or properties is unsafe")
    step = f"{service_python} -I -m agent_board.night"
    return (
        SYSTEMD_RUN,
        "--system",
        "--no-block",
        "--quiet",
        f"--unit={spec.unit}",
        f"--slice={NIGHT_SLICE}",
        "--property=Type=exec",
        "--property=Delegate=no",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=Restart=no",
        "--property=NoNewPrivileges=yes",
        f"--property=CapabilityBoundingSet={' '.join(DROP_CAPABILITIES)}",
        "--property=AmbientCapabilities=",
        "--property=ProtectSystem=strict",
        f"--property=ReadWritePaths={agent.workdir_root} {agent.happier_home} {config.state_path.parent}",
        f"--property=InaccessiblePaths={config.signer_key}",
        "--property=PrivateTmp=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=UMask=0077",
        f"--property=BindsTo={config.guard_unit}",
        f"--property=After={config.guard_unit}",
        f"--property=TimeoutStartSec={GATE_TIMEOUT_SECONDS}",
        f"--property=TimeoutStopSec={limits.stop_grace_seconds}",
        f"--property=RuntimeMaxSec={max(1, int(spec.runtime_max_seconds))}",
        f"--property=MemoryMax={limits.memory_max}",
        f"--property=TasksMax={limits.tasks_max}",
        f"--property=CPUQuota={limits.cpu_quota}",
        f"--property=ExecStartPre={step} gate --config {config_path} {spec.run_id}",
        f"--working-directory={agent.workdir_root}",
        "--",
        str(service_python),
        "-I",
        "-m",
        "agent_board.night",
        "exec",
        "--config",
        str(config_path),
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
        gate_python: Path,
    ) -> None:
        self._gate_python = gate_python
        self._config = config
        self._config_path = config_path
        self._ledger = ledger
        self._inspector = inspector
        self._commands = commands

    def launch(self, spec: RunSpec) -> None:
        self._commands.run(run_argv(self._config, self._config_path, spec, self._gate_python))

    def stop(self, unit: str) -> None:
        """Freezes and kills through the kernel first, so neither a slow manager nor a pending start beats the deadline.

        A frozen cgroup stays frozen and freezes anything that later joins it, so a pending ExecStart cannot run
        while the manager is asked to stop the unit (which cancels that start job); the final kill then removes
        whatever joined meanwhile. A manager failure still raises, so the caller treats the stop as unproven.
        """
        cgroup = CGROUP_ROOT / unit_cgroup(unit).lstrip("/")
        self._commands.freeze_cgroup(cgroup)
        self._kill(cgroup, unit)
        try:
            self._commands.run((SYSTEMCTL, "--system", "stop", "--no-block", unit))
        finally:
            self._kill(cgroup, unit)

    def _kill(self, cgroup: Path, unit: str) -> None:
        if self._commands.kill_cgroup(cgroup):
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
        return Binding(run_id, recorded.uid, recorded.boot_id, recorded.invocation_id, ROOT_EXEC)
