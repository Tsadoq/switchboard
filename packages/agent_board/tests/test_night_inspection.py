import os
from pathlib import Path
from uuid import uuid4

import pytest
from agent_board.runner.night_inspection import (
    CGROUP_ROOT,
    NIGHT_SLICE,
    Binding,
    InspectionError,
    Inspector,
    LiveHost,
)


class Host:
    def __init__(self) -> None:
        self.binding = Binding(str(uuid4()), 1000, str(uuid4()), "a" * 32)
        self.boot = self.binding.boot_id
        self.props = {
            "Id": self.binding.unit,
            "LoadState": "loaded",
            "ActiveState": "active",
            "User": "1000",
            "Slice": NIGHT_SLICE,
            "Delegate": "no",
            "Restart": "no",
            "InvocationID": self.binding.invocation_id,
            "ControlGroup": self.binding.cgroup,
            "Job": "0",
        }
        group = CGROUP_ROOT / self.binding.cgroup.lstrip("/")
        self.metadata_values = {
            path: metadata(0o40755)
            for path in (
                Path("/"),
                Path("/sys"),
                Path("/sys/fs"),
                CGROUP_ROOT,
                CGROUP_ROOT / NIGHT_SLICE,
                group,
            )
        }
        for path in (CGROUP_ROOT, CGROUP_ROOT / NIGHT_SLICE, group):
            self.metadata_values[path / "cgroup.procs"] = metadata(0o100644)
        self.metadata_values[CGROUP_ROOT / "cgroup.controllers"] = metadata(0o100444)
        self.metadata_values[group / "cgroup.events"] = metadata(0o100444)
        self.contents = {
            CGROUP_ROOT / "cgroup.controllers": "cpu memory pids\n",
            group / "cgroup.events": "populated 0\nfrozen 0\n",
        }
        self.calls = 0
        self.change_after_first = False

    def boot_id(self) -> str:
        return self.boot

    def properties(self, unit: str) -> dict[str, str]:
        assert unit == self.binding.unit
        self.calls += 1
        if self.change_after_first and self.calls > 1:
            return self.props | {"InvocationID": "b" * 32}
        return dict(self.props)

    def metadata(self, path: Path) -> os.stat_result | None:
        return self.metadata_values.get(path)

    def read(self, path: Path) -> str:
        return self.contents[path]


def metadata(mode: int, uid: int = 0) -> os.stat_result:
    return os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))


def capture(host: Host) -> Binding:
    return Inspector(host).capture(
        run_id=host.binding.run_id, uid=host.binding.uid, boot_id=host.binding.boot_id
    )


@pytest.mark.parametrize("field,value", [("run_id", "../unrelated"), ("uid", 0), ("boot_id", "invalid")])
def test_invalid_binding_never_reaches_manager(
    field: str, value: str | int, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host()

    def forbidden(unit: str) -> dict[str, str]:
        raise AssertionError("invalid identity reached manager")

    monkeypatch.setattr(host, "properties", forbidden)
    values = {"run_id": host.binding.run_id, "uid": host.binding.uid, "boot_id": host.binding.boot_id}
    values[field] = value
    with pytest.raises(InspectionError):
        Inspector(host).capture(
            run_id=str(values["run_id"]), uid=int(str(values["uid"])), boot_id=str(values["boot_id"])
        )


def test_capture_and_inactive_empty_observation() -> None:
    host = Host()
    binding = capture(host)
    host.props["ActiveState"] = "inactive"
    host.props["Job"] = ""
    result = Inspector(host).stopped(binding)
    assert result.binding == binding and result.cgroup_present and result.unit_loaded


@pytest.mark.parametrize(
    "field,value",
    [
        ("User", "0"),
        ("User", "1001"),
        ("Delegate", "yes"),
        ("Restart", "always"),
        ("Slice", "user.slice"),
        ("ControlGroup", "/user.slice/escaped"),
        ("InvocationID", "0" * 32),
        ("Id", "unrelated.service"),
        ("LoadState", "not-found"),
        ("ActiveState", "inactive"),
    ],
)
def test_capture_rejects_wrong_system_unit_binding(field: str, value: str) -> None:
    host = Host()
    host.props[field] = value
    with pytest.raises(InspectionError):
        capture(host)


@pytest.mark.parametrize("location", ["unit", "slice", "root", "common_procs"])
@pytest.mark.parametrize("bad", ["owner", "writable", "symlink"])
def test_capture_rejects_delegated_or_replaceable_paths(location: str, bad: str) -> None:
    host = Host()
    paths = {
        "unit": CGROUP_ROOT / host.binding.cgroup.lstrip("/"),
        "slice": CGROUP_ROOT / NIGHT_SLICE,
        "root": CGROUP_ROOT,
        "common_procs": CGROUP_ROOT / "cgroup.procs",
    }
    path = paths[location]
    old = host.metadata_values[path]
    host.metadata_values[path] = metadata(
        0o120777 if bad == "symlink" else old.st_mode | (0o020 if bad == "writable" else 0),
        1000 if bad == "owner" else 0,
    )
    with pytest.raises(InspectionError, match="untrusted_cgroup_path"):
        capture(host)


@pytest.mark.parametrize(
    "events", ["populated 1\nfrozen 0\n", "", "frozen 0\n", "populated 0\npopulated 1\n", "populated\n"]
)
def test_inactive_alone_does_not_prove_empty(events: str) -> None:
    host = Host()
    binding = capture(host)
    host.props["ActiveState"] = "inactive"
    host.contents[CGROUP_ROOT / binding.cgroup.lstrip("/") / "cgroup.events"] = events
    with pytest.raises(InspectionError):
        Inspector(host).stopped(binding)


@pytest.mark.parametrize("unloaded", [False, True])
def test_absent_cgroup_needs_prior_binding_and_trusted_manager_state(unloaded: bool) -> None:
    host = Host()
    binding = capture(host)
    host.props["ActiveState"] = "inactive"
    host.metadata_values.pop(CGROUP_ROOT / binding.cgroup.lstrip("/"))
    if unloaded:
        host.props.update(LoadState="not-found", InvocationID="", ControlGroup="")
    result = Inspector(host).stopped(binding)
    assert not result.cgroup_present and result.unit_loaded == (not unloaded)
    with pytest.raises(InspectionError):
        capture(host)


@pytest.mark.parametrize(
    "field,value", [("InvocationID", "b" * 32), ("Job", "42"), ("ActiveState", "active"), ("Delegate", "yes")]
)
def test_stop_refuses_reuse_or_pending_work(field: str, value: str) -> None:
    host = Host()
    binding = capture(host)
    host.props["ActiveState"] = "inactive"
    host.props[field] = value
    with pytest.raises(InspectionError):
        Inspector(host).stopped(binding)


def test_changed_boot_and_mid_observation_invocation_changes_fail_closed() -> None:
    host = Host()
    host.change_after_first = True
    with pytest.raises(InspectionError, match="unstable_binding"):
        capture(host)
    host.change_after_first = False
    binding = capture(host)
    host.boot = str(uuid4())
    with pytest.raises(InspectionError, match="boot_changed"):
        Inspector(host).stopped(binding)
    host.boot = binding.boot_id
    host.props["ActiveState"] = "inactive"
    host.calls = 0
    host.change_after_first = True
    with pytest.raises(InspectionError, match="unstable_stop"):
        Inspector(host).stopped(binding)


def test_live_backend_requires_root_before_any_manager_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(InspectionError, match="root_required"):
        LiveHost()
