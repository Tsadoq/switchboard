import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from agent_board import identity
from agent_board.config import ConfigError, board_home
from agent_board.runner.weekly import WeeklySlot

RUNNER_CONF_ENV = "AGENT_BOARD_RUNNER_CONF"
SYSTEM_CONFIG = Path("/etc/agent-board-runner.toml")
USER_CONFIG_NAME = "runner.toml"
NO_FALLBACK = "none"
REMOTE_CONTROL = "remote-control"
PERMISSION_MODES = ("read_only", "default", "auto", "yolo")
# The board holds a wake poll open for at most 60 s.
MAX_POLL_WAIT_SECONDS = 60
MAX_COALESCE_SECONDS = 180
_KIND_TABLE = "kind"
_GLOBAL_KEYS = {"kinds", "coalesce_seconds", "poll_wait", "notify_human", "session_home", _KIND_TABLE}
_KIND_KEYS = {"workdir", "permission_mode", "backend", "fallback", "models", "weekly"}
MODEL_KEYS = ("default", "mention", "follow", "timer", "weekly")

type Table = dict[str, Any]


@dataclass(frozen=True)
class KindConfig:
    name: str
    workdir: Path
    permission_mode: str
    backend: str
    fallback: str
    models: Mapping[str, str] = field(default_factory=dict)
    weekly: WeeklySlot | None = None


@dataclass(frozen=True)
class RunnerConfig:
    kinds: Mapping[str, KindConfig]
    coalesce_seconds: float
    poll_wait: float
    notify_human: bool
    session_home: Path | None = None

    @classmethod
    def load(cls) -> Self:
        system = Path(os.environ.get(RUNNER_CONF_ENV) or SYSTEM_CONFIG)
        raw = _merged(_read(system), _read(board_home() / USER_CONFIG_NAME))
        _reject_unknown(raw, _GLOBAL_KEYS, "")
        names = _kind_names(_value(raw, "kinds", [])) if "kinds" in raw else identity.kinds()
        tables: dict[str, Table] = _value(raw, _KIND_TABLE, {})
        return cls(
            kinds={name: _kind(name, tables.get(name, {})) for name in names},
            coalesce_seconds=_seconds(raw, "coalesce_seconds", 120, maximum=MAX_COALESCE_SECONDS),
            poll_wait=_seconds(raw, "poll_wait", 30, maximum=MAX_POLL_WAIT_SECONDS, positive=True),
            notify_human=_value(raw, "notify_human", True),
            session_home=_session_home(_value(raw, "session_home", "")),
        )


def _read(path: Path) -> Table:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from None


def _merged(system: Table, user: Table) -> Table:
    system_kinds: Table = _value(system, _KIND_TABLE, {})
    user_kinds: Table = _value(user, _KIND_TABLE, {})
    where = f"{_KIND_TABLE}."
    kinds: dict[str, Table] = {
        name: {**_value(system_kinds, name, {}, where), **_value(user_kinds, name, {}, where)}
        for name in system_kinds | user_kinds
    }
    return {**system, **user, _KIND_TABLE: kinds}


def _kind_names(configured: list[Any]) -> list[str]:
    if not all(isinstance(name, str) and name for name in configured):
        raise ConfigError(f"runner config: kinds must be a list of kind names, got {configured!r}")
    return configured


def _kind(name: str, table: Table) -> KindConfig:
    where = f"{_KIND_TABLE}.{name}."
    _reject_unknown(table, _KIND_KEYS, where)
    permission_mode = _value(table, "permission_mode", "default", where)
    fallback = _value(table, "fallback", NO_FALLBACK, where)
    if permission_mode not in PERMISSION_MODES:
        raise ConfigError(
            f"runner config: {where}permission_mode must be one of {', '.join(PERMISSION_MODES)}"
        )
    if fallback not in (NO_FALLBACK, REMOTE_CONTROL):
        raise ConfigError(f"runner config: {where}fallback must be {NO_FALLBACK} or {REMOTE_CONTROL}")
    return KindConfig(
        name=name,
        workdir=_workdir(_value(table, "workdir", "", where), name, where),
        permission_mode=permission_mode,
        backend=_value(table, "backend", f"agent:{name}", where),
        fallback=fallback,
        models=_models(_value(table, "models", {}, where), where),
        weekly=_weekly(_value(table, "weekly", "", where), where),
    )


def _models(table: Table, where: str) -> dict[str, str]:
    where = f"{where}models."
    _reject_unknown(table, set(MODEL_KEYS), where)
    models = {key: _value(table, key, "", where).strip() for key in table}
    empty = sorted(key for key, model in models.items() if not model)
    if empty:
        raise ConfigError(f"runner config: {where}{empty[0]} must name a model")
    return models


def _weekly(configured: str, where: str) -> WeeklySlot | None:
    return WeeklySlot.parse(configured, where) if configured else None


def _workdir(configured: str, name: str, where: str) -> Path:
    home = Path.home()
    workdir = Path(configured).expanduser() if configured else home / "agent-board-work" / name
    if not workdir.is_absolute():
        raise ConfigError(
            f"runner config: {where}workdir must be absolute or start with ~, got {configured!r}"
        )
    if workdir.resolve() == home.resolve():
        raise ConfigError(
            f"runner config: {where}workdir is $HOME ({home}); Claude never saves directory trust there, "
            "so use a subdirectory"
        )
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigError(f"runner config: cannot create {where}workdir {workdir}: {error}") from None
    return workdir


def _session_home(configured: str) -> Path | None:
    if not configured:
        return None
    home = Path(configured).expanduser()
    if not home.is_absolute():
        raise ConfigError(f"runner config: session_home must be absolute or start with ~, got {configured!r}")
    return home


def _seconds(table: Table, key: str, default: float, *, maximum: float, positive: bool = False) -> float:
    value = table.get(key, default)
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    if not is_number or not 0 <= value <= maximum or (positive and value == 0):
        lowest = "above 0" if positive else "at least 0"
        raise ConfigError(f"runner config: {key} must be a number of seconds {lowest} and at most {maximum}")
    return float(value)


def _value[T](table: Table, key: str, default: T, where: str = "") -> T:
    value = table.get(key, default)
    if not isinstance(value, type(default)):
        raise ConfigError(f"runner config: {where}{key} must be a {type(default).__name__}, got {value!r}")
    return value


def _reject_unknown(table: Table, known: set[str], where: str) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"runner config: unknown keys {', '.join(where + key for key in unknown)}")
