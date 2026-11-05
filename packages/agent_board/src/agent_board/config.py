import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

BOARD_URL_ENV = "AGENT_BOARD_URL"
_INSTALL_HINT = (
    f"pass --board URL, set {BOARD_URL_ENV}, or run `agent-board init --board URL` "
    "(the installer does this for you)"
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    board_url: str


def board_home() -> Path:
    return Path.home() / ".agents" / "board"


def config_path() -> Path:
    return board_home() / "config.toml"


def load(board_url: str | None = None) -> Config:
    url = board_url or os.environ.get(BOARD_URL_ENV) or _configured_url()
    if not url:
        raise ConfigError(f"no board URL configured: {_INSTALL_HINT}")
    return Config(url.rstrip("/"))


def write_if_absent(board_url: str) -> bool:
    path = config_path()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"board_url = {json.dumps(board_url.rstrip('/'))}\n")
    return True


def _configured_url() -> str | None:
    path = config_path()
    try:
        url = tomllib.loads(path.read_text()).get("board_url")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from None
    if not isinstance(url, str):
        raise ConfigError(f"{path} has no board_url string: {_INSTALL_HINT}")
    return url
