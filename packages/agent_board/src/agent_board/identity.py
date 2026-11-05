import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_board.config import ConfigError, board_home

HELPER = "agent-sign"
HELPER_REFUSED_EXIT = 2
IDENTITY_CONF_ENV = "AGENT_IDENTITY_CONF"
DEFAULT_IDENTITY_CONF = Path("/etc/agent-identity.conf")
_HELPER_TIMEOUT_SECONDS = 30
_STATE_DIR_MODE = 0o700


class IdentityRefused(Exception):
    def __init__(self, stderr: str) -> None:
        super().__init__(stderr)
        self.stderr = stderr


class HelperFailed(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    principal: str
    kind: str
    box: str


def resolve(as_kind: str | None = None) -> Identity:
    # `whoami` rejects --as; `pubkey --as` names the same principal as its first token (probe 24).
    output = agent_sign("whoami") if as_kind is None else agent_sign("pubkey", "--as", as_kind)
    principal = output.decode().split(maxsplit=1)[0] if output.strip() else ""
    kind, at, box = principal.partition("@")
    if not (kind and at and box):
        raise HelperFailed(f"{HELPER} reported no <kind>@<box> principal: {output!r}")
    return Identity(principal, kind, box)


def identity_conf() -> Path:
    return Path(os.environ.get(IDENTITY_CONF_ENV) or DEFAULT_IDENTITY_CONF)


def kinds(conf_path: Path | None = None) -> list[str]:
    path = conf_path or identity_conf()
    try:
        return [str(kind["name"]) for kind in json.loads(path.read_text())["kinds"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigError(f"cannot read the identity config {path}: {error!r}") from None


def state_dir(kind: str) -> Path:
    return board_home() / kind


def ensure_state_dir(kind: str) -> Path:
    path = state_dir(kind)
    path.mkdir(mode=_STATE_DIR_MODE, parents=True, exist_ok=True)
    path.chmod(_STATE_DIR_MODE)
    return path


def agent_sign(*args: str, stdin: bytes = b"") -> bytes:
    override = os.environ.get(IDENTITY_CONF_ENV)
    argv = [HELPER, *(["--config", override] if override else []), *args]
    try:
        completed = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=_HELPER_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        raise HelperFailed(f"{HELPER} is not on PATH; see 'The agent-sign helper' in the README") from None
    except subprocess.TimeoutExpired:
        raise HelperFailed(f"{HELPER} {args[0]} did not finish in {_HELPER_TIMEOUT_SECONDS}s") from None
    stderr = completed.stderr.decode(errors="replace")
    if completed.returncode == HELPER_REFUSED_EXIT:
        raise IdentityRefused(stderr)
    if completed.returncode != 0:
        raise HelperFailed(f"{HELPER} {args[0]} exited {completed.returncode}: {stderr.strip()}")
    return completed.stdout
