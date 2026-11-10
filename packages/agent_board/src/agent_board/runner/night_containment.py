import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

STOP_GRACE = 5
DEADLINE_HEADROOM = 1
MEMORY_BYTES = 600 * 1024 * 1024
TASKS = 128


class ContainmentError(ValueError):
    pass


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    fence: str
    principal: str
    issued_monotonic: float
    deadline_monotonic: float
    boot_id: str
    uid: int
    home: Path
    ordinary_home: Path
    workdir: Path
    ordinary_workdir: Path
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            valid_ids = str(UUID(self.run_id)) == self.run_id and str(UUID(self.boot_id)) == self.boot_id
        except (ValueError, TypeError, AttributeError):
            valid_ids = False
        if not valid_ids:
            raise ContainmentError("invalid_identity")
        if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.fence) is None:
            raise ContainmentError("invalid_fence")
        if re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+", self.principal) is None:
            raise ContainmentError("excluded")
        if self.principal.partition("@")[2].casefold() in ("ops", "heimdall"):
            raise ContainmentError("excluded")
        if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
            raise ContainmentError("invalid_uid")
        for value in (self.issued_monotonic, self.deadline_monotonic):
            if not math.isfinite(value) or value < 0:
                raise ContainmentError("invalid_clock")
        if not STOP_GRACE + DEADLINE_HEADROOM < self.deadline_monotonic - self.issued_monotonic <= 1200:
            raise ContainmentError("expired")
        for dedicated, ordinary in ((self.home, self.ordinary_home), (self.workdir, self.ordinary_workdir)):
            if not dedicated.is_absolute() or not ordinary.is_absolute():
                raise ContainmentError("invalid_path")
            left, right = dedicated.resolve(), ordinary.resolve()
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise ContainmentError("shared_path")
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(part, str) or not part or "\x00" in part for part in self.command)
        ):
            raise ContainmentError("invalid_command")

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        fence: str,
        principal: str,
        seconds: float,
        request_elapsed: float,
        monotonic_now: float,
        boot_id: str,
        uid: int,
        home: Path,
        ordinary_home: Path,
        workdir: Path,
        ordinary_workdir: Path,
        command: tuple[str, ...],
    ) -> "RunPlan":
        if not math.isfinite(seconds) or not 0 < seconds <= 1200:
            raise ContainmentError("invalid_budget")
        if not math.isfinite(request_elapsed) or request_elapsed < 0:
            raise ContainmentError("invalid_clock")
        return cls(
            run_id,
            fence,
            principal,
            monotonic_now,
            monotonic_now + seconds - request_elapsed,
            boot_id,
            uid,
            home,
            ordinary_home,
            workdir,
            ordinary_workdir,
            command,
        )

    @property
    def unit(self) -> str:
        return f"board-autonomy-{self.run_id}.service"

    @property
    def cgroup(self) -> str:
        return f"/user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice/{self.unit}"

    @property
    def marker(self) -> str:
        return hashlib.sha256(self.fence.encode()).hexdigest()

    def runtime_limit(self, now: float) -> float:
        if not math.isfinite(now) or now < self.issued_monotonic:
            raise ContainmentError("invalid_clock")
        seconds = math.floor((self.deadline_monotonic - now - STOP_GRACE - DEADLINE_HEADROOM) * 1000) / 1000
        if seconds <= 0:
            raise ContainmentError("expired")
        return seconds

    def save(self, path: Path) -> None:
        record = asdict(self)
        for key in ("home", "ordinary_home", "workdir", "ordinary_workdir"):
            record[key] = str(record[key])
        _write_once(path, {"v": 1, "plan": record})

    @classmethod
    def load(cls, path: Path) -> "RunPlan":
        try:
            record = json.loads(path.read_text())
            if record["v"] != 1 or not isinstance(record["plan"], dict):
                raise ContainmentError("invalid_plan")
            values = record["plan"]
            for key in ("home", "ordinary_home", "workdir", "ordinary_workdir"):
                values[key] = Path(values[key])
            values["command"] = tuple(values["command"])
            return cls(**values)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ContainmentError("invalid_plan") from None


def unit_argv(run: RunPlan, journal: Path, python: str) -> tuple[str, ...]:
    return (
        "systemd-run",
        "--user",
        "--quiet",
        f"--unit={run.unit}",
        "--property=Type=exec",
        "--property=Slice=app.slice",
        "--property=ExitType=cgroup",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=Restart=no",
        f"--property=Description=Switchboard autonomy {run.marker}",
        f"--property=TimeoutStopSec={STOP_GRACE}",
        f"--property=RuntimeMaxSec={run.runtime_limit(run.issued_monotonic)}",
        f"--property=MemoryMax={MEMORY_BYTES}",
        f"--property=TasksMax={TASKS}",
        "--property=CPUQuota=100%",
        "--property=NoNewPrivileges=yes",
        "--property=UMask=0077",
        f"--working-directory={run.workdir}",
        "--",
        python,
        "-m",
        __name__,
        "--worker",
        str(journal),
        run.unit,
    )


def verified_empty(
    run: RunPlan,
    properties: dict[str, str],
    boot_id: str,
    observed_invocation: str,
    root: Path,
) -> bool:
    if (
        boot_id != run.boot_id
        or re.fullmatch(r"[0-9a-f]{32}", observed_invocation) is None
        or observed_invocation == "0" * 32
    ):
        return False
    if properties.get("Id") != run.unit or properties.get("ActiveState") not in ("inactive", "failed"):
        return False
    if properties.get("ControlGroup") not in ("", run.cgroup):
        return False
    if properties.get("LoadState") == "not-found":
        if properties.get("InvocationID") != "" or properties.get("ControlGroup") != "":
            return False
    elif properties.get("LoadState") == "loaded":
        if properties.get("InvocationID") not in ("", observed_invocation):
            return False
        if properties.get("Description") != f"Switchboard autonomy {run.marker}":
            return False
    else:
        return False
    try:
        if "pids" not in (root / "cgroup.controllers").read_text().split():
            return False
    except OSError:
        return False
    group = root / run.cgroup.lstrip("/")
    try:
        events = (group / "cgroup.events").read_text()
    except FileNotFoundError:
        try:
            group.stat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False
    except OSError:
        return False
    return (
        dict(line.split() for line in events.splitlines() if len(line.split()) == 2).get("populated") == "0"
    )


def worker(journal: Path, unit: str) -> None:
    run = RunPlan.load(journal)
    boot_id, group = _kernel_identity()
    invocation = os.environ.get("INVOCATION_ID", "")
    if unit != run.unit or os.getuid() != run.uid or boot_id != run.boot_id:
        raise ContainmentError("wrong_host_or_unit")
    if group != f"0::{run.cgroup}" or re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
        raise ContainmentError("wrong_cgroup")
    run.runtime_limit(time.monotonic())
    _write_once(
        journal.with_suffix(".observed.json"),
        {
            "v": 1,
            "run_id": run.run_id,
            "marker": run.marker,
            "boot_id": boot_id,
            "cgroup": run.cgroup,
            "invocation": invocation,
        },
    )
    _checked(deadline_argv(run))
    run.runtime_limit(time.monotonic())
    env = {**os.environ, "HAPPIER_HOME_DIR": str(run.home)}
    os.execvpe(run.command[0], run.command, env)


def deadline_argv(run: RunPlan) -> tuple[str, ...]:
    stop_at = math.floor((run.deadline_monotonic - STOP_GRACE - DEADLINE_HEADROOM) * 1000) / 1000
    return (
        "systemd-run",
        "--user",
        "--quiet",
        f"--unit={run.unit.removesuffix('.service')}-deadline",
        f"--on-boot={stop_at}s",
        "--timer-property=AccuracySec=1ms",
        "--timer-property=RandomizedDelaySec=0",
        "--property=Type=oneshot",
        f"--property=TimeoutStartSec={STOP_GRACE + 2}",
        "--",
        "/usr/bin/systemctl",
        "--user",
        "stop",
        run.unit,
    )


def _kernel_identity() -> tuple[str, str]:
    return (
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        Path("/proc/self/cgroup").read_text().strip(),
    )


def _checked(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    if completed.returncode != 0:
        raise ContainmentError(f"unit_command_failed:{completed.returncode}:{completed.stderr.strip()[:256]}")
    return completed


def _write_once(path: Path, record: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(record, file)
        file.flush()
        os.fsync(file.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    worker(Path(sys.argv[2]), sys.argv[3])
