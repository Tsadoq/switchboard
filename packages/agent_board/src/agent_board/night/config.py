import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_board.night_inspection import NIGHT_SLICE

_BOX = re.compile(r"[a-z0-9_-]+")
_PRINCIPAL = re.compile(r"[A-Za-z0-9._-]+@[a-z0-9_-]+")
_SLICE = re.compile(r"[a-z0-9_-]+\.slice")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
_EXCLUDED_BOXES = ("ops", "heimdall")
PRODUCTION_GUARD_UNIT = "switchboard-night-guard.service"
PRODUCTION_VERIFIER_UNIT = "switchboard-night-verifier.service"
PRODUCTION_VERIFIER_SOCKET = "switchboard-night-verifier.socket"
ACCEPTANCE_STUB = "agent_board.night.acceptance_stub"
_GUARD_UNIT = re.compile(r"switchboard-night-guard(?:-[a-z0-9]{1,32})?\.service")
_VERIFIER_UNIT = re.compile(r"switchboard-night-verifier(?:-[a-z0-9]{1,32})?\.service")
_VERIFIER_SOCKET = re.compile(r"switchboard-night-verifier(?:-[a-z0-9]{1,32})?\.socket")
_TOP_KEYS = {
    "box",
    "principal",
    "namespace",
    "board_url",
    "ca_path",
    "signer_key",
    "state_path",
    "slice",
    "launcher",
    "agents",
    "limits",
    "guard_unit",
    "verifier_unit",
    "verifier_socket",
}
_LAUNCHER_KEYS = {"python", "module", "happier"}
_AGENT_KEYS = {"uid", "home", "happier_home", "workdir_root"}
_LIMIT_KEYS = {"memory_max", "tasks_max", "cpu_quota", "stop_grace_seconds"}
_MEMORY = re.compile(r"[1-9][0-9]*[KMG]")
_QUOTA = re.compile(r"[1-9][0-9]*%")


class VerifierConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Agent:
    principal: str
    uid: int
    home: Path
    happier_home: Path
    workdir_root: Path


@dataclass(frozen=True)
class Limits:
    memory_max: str
    tasks_max: int
    cpu_quota: str
    stop_grace_seconds: int


@dataclass(frozen=True)
class VerifierConfig:
    box: str
    principal: str
    namespace: str
    board_url: str
    ca_path: Path | None
    signer_key: Path
    state_path: Path
    slice: str
    launcher_python: Path
    launcher_module: str
    agents: Mapping[str, Agent]
    limits: Limits
    launcher_happier: Path | None = None
    guard_unit: str = PRODUCTION_GUARD_UNIT
    verifier_unit: str = PRODUCTION_VERIFIER_UNIT
    verifier_socket: str = PRODUCTION_VERIFIER_SOCKET

    def principals_for_uid(self, uid: int) -> frozenset[str]:
        """Several agent kinds may share one OS account, so a peer uid can only narrow, never pick, the principal."""
        return frozenset(agent.principal for agent in self.agents.values() if agent.uid == uid)


def load(path: Path) -> VerifierConfig:
    """Reads the config only after it and every ancestor are proven root-controlled, then checks the launcher."""
    _absolute(path, "config")
    trusted_file(path)
    try:
        table = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise VerifierConfigError(f"unreadable config {path}: {error}") from None
    config = parse(table)
    trusted_file(config.launcher_python)
    if config.launcher_happier is not None:
        trusted_file(config.launcher_happier)
    return config


def trusted_file(path: Path) -> None:
    """Fails unless `path` is a regular root-owned file, not group/other writable, under such directories.

    No symlink is accepted anywhere, not even for an interpreter: release venvs are created with --copies,
    so nothing root reads or executes can be retargeted through a link.
    """
    _trusted_directories(path.parent)
    metadata = _metadata(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise VerifierConfigError(f"{path} must be a regular root-owned file that only root can write")


def _trusted_directories(directory: Path) -> None:
    for each in (directory, *directory.parents):
        metadata = _metadata(each)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise VerifierConfigError(f"{each} must be a root-owned directory that only root can write")


def _metadata(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise VerifierConfigError(f"cannot inspect {path}: {error}") from None


def parse(table: Mapping[str, Any]) -> VerifierConfig:
    _keys(
        table, _TOP_KEYS, "top level", optional={"ca_path", "guard_unit", "verifier_unit", "verifier_socket"}
    )
    box = _match(table["box"], _BOX, "box")
    if box in _EXCLUDED_BOXES:
        raise VerifierConfigError(f"box {box} is excluded from night runs")
    principal = _match(table["principal"], _PRINCIPAL, "principal")
    if principal != f"verifier@{box}":
        raise VerifierConfigError("principal must be verifier@<box>")
    board_url = _string(table["board_url"], "board_url")
    if not board_url.startswith("https://"):
        raise VerifierConfigError("board_url must be https")
    launcher = _table(table["launcher"], "launcher")
    _keys(launcher, _LAUNCHER_KEYS, "launcher", optional={"happier"})
    agents_table = _table(table["agents"], "agents")
    if not agents_table:
        raise VerifierConfigError("agents must name at least one principal")
    agents = {name: _agent(name, value, box) for name, value in agents_table.items()}
    return VerifierConfig(
        box=box,
        principal=principal,
        namespace=_match(table["namespace"], _NAMESPACE, "namespace"),
        board_url=board_url,
        ca_path=_absolute(table["ca_path"], "ca_path") if "ca_path" in table else None,
        signer_key=_absolute(table["signer_key"], "signer_key"),
        state_path=_absolute(table["state_path"], "state_path"),
        slice=_night_slice(table["slice"]),
        launcher_python=_absolute(launcher["python"], "launcher.python"),
        launcher_module=_match(launcher["module"], _MODULE, "launcher.module"),
        launcher_happier=_absolute(launcher["happier"], "launcher.happier")
        if "happier" in launcher
        else None,
        agents=agents,
        limits=_limits(_table(table["limits"], "limits")),
        guard_unit=_unit_name(
            table.get("guard_unit", PRODUCTION_GUARD_UNIT),
            _GUARD_UNIT,
            PRODUCTION_GUARD_UNIT,
            "guard_unit",
            launcher,
        ),
        verifier_unit=_unit_name(
            table.get("verifier_unit", PRODUCTION_VERIFIER_UNIT),
            _VERIFIER_UNIT,
            PRODUCTION_VERIFIER_UNIT,
            "verifier_unit",
            launcher,
        ),
        verifier_socket=_unit_name(
            table.get("verifier_socket", PRODUCTION_VERIFIER_SOCKET),
            _VERIFIER_SOCKET,
            PRODUCTION_VERIFIER_SOCKET,
            "verifier_socket",
            launcher,
        ),
    )


def _agent(principal: str, value: Any, box: str) -> Agent:
    _match(principal, _PRINCIPAL, "agent principal")
    if principal.partition("@")[2] != box or principal.partition("@")[0].casefold() == "verifier":
        raise VerifierConfigError(f"agent {principal} does not belong to box {box}")
    table = _table(value, f"agents.{principal}")
    _keys(table, _AGENT_KEYS, f"agents.{principal}")
    uid = table["uid"]
    if type(uid) is not int or uid <= 0:
        raise VerifierConfigError(f"agents.{principal}.uid must be a positive integer")
    return Agent(
        principal,
        uid,
        _absolute(table["home"], f"agents.{principal}.home"),
        _absolute(table["happier_home"], f"agents.{principal}.happier_home"),
        _absolute(table["workdir_root"], f"agents.{principal}.workdir_root"),
    )


def _unit_name(
    value: Any, pattern: re.Pattern[str], production: str, where: str, launcher: Mapping[str, Any]
) -> str:
    """A non-production unit name is only for the acceptance smoke, so it requires the inert stub launcher."""
    unit = _match(value, pattern, where)
    if unit != production and launcher.get("module") != ACCEPTANCE_STUB:
        raise VerifierConfigError(f"{where} may only differ from {production} with the acceptance stub")
    return unit


def _night_slice(value: Any) -> str:
    text = _match(value, _SLICE, "slice")
    if text != NIGHT_SLICE:
        raise VerifierConfigError(f"slice must be {NIGHT_SLICE}, which the inspector verifies")
    return text


def _limits(table: Mapping[str, Any]) -> Limits:
    _keys(table, _LIMIT_KEYS, "limits")
    tasks, grace = table["tasks_max"], table["stop_grace_seconds"]
    if type(tasks) is not int or not 1 <= tasks <= 4096:
        raise VerifierConfigError("limits.tasks_max must be an integer from 1 to 4096")
    if type(grace) is not int or not 1 <= grace <= 30:
        raise VerifierConfigError("limits.stop_grace_seconds must be an integer from 1 to 30")
    return Limits(
        _match(table["memory_max"], _MEMORY, "limits.memory_max"),
        tasks,
        _match(table["cpu_quota"], _QUOTA, "limits.cpu_quota"),
        grace,
    )


def _keys(table: Mapping[str, Any], allowed: set[str], where: str, optional: set[str] | None = None) -> None:
    unknown = set(table) - allowed
    missing = allowed - (optional or set()) - set(table)
    if unknown or missing:
        raise VerifierConfigError(f"{where}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}")


def _table(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerifierConfigError(f"{where} must be a table")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerifierConfigError(f"{where} must be a non-empty string")
    return value


def _match(value: Any, pattern: re.Pattern[str], where: str) -> str:
    text = _string(value, where)
    if pattern.fullmatch(text) is None:
        raise VerifierConfigError(f"{where} is not valid: {text!r}")
    return text


def _absolute(value: Any, where: str) -> Path:
    text = _string(value, where) if not isinstance(value, Path) else str(value)
    path = Path(text)
    if not path.is_absolute() or os.path.normpath(text) != text or "\x00" in text:
        raise VerifierConfigError(f"{where} must be a normalized absolute path: {text!r}")
    return path
