import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from board.constants import DEFAULT_NAMESPACE

_NAMESPACE = re.compile(r"[A-Za-z0-9._@+-]{1,128}")


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    db_path: Path
    public_url: str
    human_password_hash: str | None
    post_max_kb: int
    file_max_mb: int
    file_ttl_days: int
    token_ttl_hours: int
    ts_window_seconds: int
    wake_lease_seconds: int
    dist_dir: Path
    ssh_keygen: str
    listen: str
    signing_namespace: str = DEFAULT_NAMESPACE
    update_status_path: Path = Path("/var/lib/agent-board-update/status.json")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Self:
        state_dir = Path(_text(env, "BOARD_STATE_DIR", "/var/lib/agent-board"))
        return cls(
            state_dir=state_dir,
            db_path=Path(_text(env, "BOARD_DB_PATH", str(state_dir / "board.sqlite3"))),
            public_url=_text(env, "BOARD_PUBLIC_URL", "http://127.0.0.1:8080"),
            human_password_hash=env.get("BOARD_HUMAN_PASSWORD_HASH") or None,
            post_max_kb=_integer(env, "BOARD_POST_MAX_KB", 64),
            file_max_mb=_integer(env, "BOARD_FILE_MAX_MB", 50),
            file_ttl_days=_integer(env, "BOARD_FILE_TTL_DAYS", 7),
            token_ttl_hours=_integer(env, "BOARD_TOKEN_TTL_HOURS", 12),
            ts_window_seconds=_integer(env, "BOARD_TS_WINDOW_SECONDS", 300),
            wake_lease_seconds=_integer(env, "BOARD_WAKE_LEASE_SECONDS", 300),
            dist_dir=Path(_text(env, "BOARD_DIST_DIR", "/app/dist")),
            ssh_keygen=_text(env, "BOARD_SSH_KEYGEN", "ssh-keygen"),
            listen=_text(env, "BOARD_LISTEN", "0.0.0.0:8080"),
            signing_namespace=_namespace(env),
            update_status_path=Path(
                _text(env, "BOARD_UPDATE_STATUS", "/var/lib/agent-board-update/status.json")
            ),
        )


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get(name) or default


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _namespace(env: Mapping[str, str]) -> str:
    value = _text(env, "BOARD_SIGNING_NAMESPACE", DEFAULT_NAMESPACE)
    if not _NAMESPACE.fullmatch(value):
        raise ValueError(f"BOARD_SIGNING_NAMESPACE must be 1 to 128 of A-Z a-z 0-9 . _ @ + -, got {value!r}")
    return value
