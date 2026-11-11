import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

CGROUP_ROOT = Path("/sys/fs/cgroup")
NIGHT_SLICE = "switchboard-night.slice"
PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "User",
    "Slice",
    "Delegate",
    "Restart",
    "InvocationID",
    "ControlGroup",
    "Job",
)


class InspectionError(ValueError):
    pass


class Host(Protocol):
    def boot_id(self) -> str: ...
    def properties(self, unit: str) -> dict[str, str]: ...
    def metadata(self, path: Path) -> os.stat_result | None: ...
    def read(self, path: Path) -> str: ...


@dataclass(frozen=True)
class Binding:
    run_id: str
    uid: int
    boot_id: str
    invocation_id: str

    def __post_init__(self) -> None:
        try:
            valid = str(UUID(self.run_id)) == self.run_id and str(UUID(self.boot_id)) == self.boot_id
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or type(self.uid) is not int or self.uid <= 0:
            raise InspectionError("invalid_binding")
        if re.fullmatch(r"[0-9a-f]{32}", self.invocation_id) is None or self.invocation_id == "0" * 32:
            raise InspectionError("invalid_invocation")

    @property
    def unit(self) -> str:
        return f"switchboard-night-{self.run_id}.service"

    @property
    def cgroup(self) -> str:
        return f"/{NIGHT_SLICE}/{self.unit}"


@dataclass(frozen=True)
class EmptyObservation:
    binding: Binding
    cgroup_present: bool
    unit_loaded: bool


class LiveHost:
    def __init__(self) -> None:
        if os.geteuid() != 0:
            raise InspectionError("root_required")

    def boot_id(self) -> str:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def properties(self, unit: str) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", "--system", "show", unit, "--property=" + ",".join(PROPERTIES)],
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise InspectionError("manager_unavailable") from None
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in PROPERTIES or key in values:
                raise InspectionError("invalid_manager_observation")
            values[key] = value
        if set(values) != set(PROPERTIES):
            raise InspectionError("incomplete_manager_observation")
        return values

    def metadata(self, path: Path) -> os.stat_result | None:
        try:
            return path.lstat()
        except FileNotFoundError:
            return None

    def read(self, path: Path) -> str:
        return path.read_text()


class Inspector:
    def __init__(self, host: Host) -> None:
        self._host = host

    def capture(self, *, run_id: str, uid: int, boot_id: str) -> Binding:
        identity = Binding(run_id, uid, boot_id, "f" * 32)
        properties = self._host.properties(identity.unit)
        binding = Binding(run_id, uid, boot_id, properties.get("InvocationID", ""))
        if self._host.boot_id() != boot_id or not self._matches(binding, properties):
            raise InspectionError("binding_conflict")
        if properties.get("ActiveState") not in ("active", "activating"):
            raise InspectionError("not_active")
        if not self._protected_cgroup(binding, required=True):
            raise InspectionError("missing_cgroup")
        if self._host.boot_id() != boot_id or self._host.properties(binding.unit) != properties:
            raise InspectionError("unstable_binding")
        return binding

    def stopped(self, binding: Binding) -> EmptyObservation:
        if self._host.boot_id() != binding.boot_id:
            raise InspectionError("boot_changed")
        before = self._host.properties(binding.unit)
        loaded = before.get("LoadState") == "loaded"
        if loaded:
            if not self._matches(binding, before, stopped=True):
                raise InspectionError("binding_conflict")
        elif before.get("LoadState") != "not-found" or before.get("InvocationID") not in ("", "0" * 32):
            raise InspectionError("unknown_unit")
        if before.get("ActiveState") not in ("inactive", "failed") or before.get("Job") not in ("", "0"):
            raise InspectionError("unit_not_stopped")
        present = self._protected_cgroup(binding, required=False)
        if present:
            events = CGROUP_ROOT / binding.cgroup.lstrip("/") / "cgroup.events"
            self._protected(events, directory=False)
            values: dict[str, str] = {}
            for line in self._host.read(events).splitlines():
                fields = line.split()
                if len(fields) != 2 or fields[0] in values:
                    raise InspectionError("invalid_cgroup_events")
                values[fields[0]] = fields[1]
            if values.get("populated") != "0":
                raise InspectionError("populated_or_unknown")
        if self._host.boot_id() != binding.boot_id or self._host.properties(binding.unit) != before:
            raise InspectionError("unstable_stop")
        return EmptyObservation(binding, present, loaded)

    def _protected_cgroup(self, binding: Binding, *, required: bool) -> bool:
        for parent in reversed((CGROUP_ROOT, *CGROUP_ROOT.parents)):
            self._protected(parent, directory=True)
        self._protected(CGROUP_ROOT / "cgroup.controllers", directory=False)
        self._host.read(CGROUP_ROOT / "cgroup.controllers")
        present = True
        for group in (CGROUP_ROOT, CGROUP_ROOT / NIGHT_SLICE, CGROUP_ROOT / binding.cgroup.lstrip("/")):
            if self._host.metadata(group) is None:
                if required:
                    raise InspectionError("missing_cgroup")
                present = False
                break
            self._protected(group, directory=True)
            self._protected(group / "cgroup.procs", directory=False)
        return present

    def _protected(self, path: Path, *, directory: bool) -> None:
        metadata = self._host.metadata(path)
        correct_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            metadata is None
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
            or not correct_type(metadata.st_mode)
        ):
            raise InspectionError("untrusted_cgroup_path")

    @staticmethod
    def _matches(binding: Binding, properties: dict[str, str], *, stopped: bool = False) -> bool:
        return (
            properties.get("Id") == binding.unit
            and properties.get("LoadState") == "loaded"
            and properties.get("User") == str(binding.uid)
            and properties.get("Slice") == NIGHT_SLICE
            and properties.get("Delegate") == "no"
            and properties.get("Restart") == "no"
            and properties.get("InvocationID") == binding.invocation_id
            and properties.get("ControlGroup") in (("", binding.cgroup) if stopped else (binding.cgroup,))
        )
