import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from board import night, night_verifier, passwords
from board.app import create_app
from board.db import Database
from board.settings import Settings


@dataclass(frozen=True)
class SmokeConfig:
    directory: Path
    box: str
    principal: str
    starts_at: float
    duration_seconds: int
    tls_cert: Path
    tls_key: Path
    verifier_public_key: Path
    port: int

    def window(self, now: float) -> night.NightWindow | None:
        end = self.starts_at + self.duration_seconds
        if not self.starts_at <= now < end:
            return None
        return night.NightWindow(
            datetime.fromtimestamp(self.starts_at, UTC).date().isoformat(), self.starts_at, end
        )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_fixture_field")
        result[key] = value
    return result


def _protected(path: Path, *, private: bool) -> None:
    if not path.is_absolute() or path.anchor != "/" or path != Path(os.path.normpath(path)):
        raise ValueError("unsafe_fixture_path")
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unsafe_fixture_ancestor")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or info.st_mode & (0o077 if private else 0o022)
    ):
        raise ValueError("unsafe_fixture_file")


def load(path: Path) -> SmokeConfig:
    _protected(path, private=True)
    directory = path.parent
    if directory.stat().st_mode & 0o077:
        raise ValueError("fixture_directory_not_private")
    data = json.loads(path.read_text(), object_pairs_hook=_unique)
    expected = {
        "box",
        "principal",
        "starts_at",
        "duration_seconds",
        "tls_cert",
        "tls_key",
        "verifier_public_key",
        "port",
    }
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("invalid_fixture_fields")
    box, principal = data["box"], data["principal"]
    if not isinstance(box, str) or re.fullmatch(r"[a-z0-9_-]+", box) is None or box in ("ops", "heimdall"):
        raise ValueError("invalid_fixture_box")
    if (
        not isinstance(principal, str)
        or re.fullmatch(r"[A-Za-z0-9._-]+@" + re.escape(box), principal) is None
        or principal.partition("@")[0].casefold() in ("verifier", "heimdall")
    ):
        raise ValueError("invalid_fixture_principal")
    start, duration, port = data["starts_at"], data["duration_seconds"], data["port"]
    try:
        valid_start = type(start) in (int, float) and math.isfinite(start) and start >= 0
    except OverflowError:
        valid_start = False
    if (
        not valid_start
        or type(duration) is not int
        or not 1 <= duration <= 120
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise ValueError("invalid_fixture_limits")
    files = []
    for name in ("tls_cert", "tls_key", "verifier_public_key"):
        if not isinstance(data[name], str):
            raise TypeError("invalid_fixture_path")
        asset = Path(data[name])
        if asset.parent != directory:
            raise ValueError("fixture_asset_outside_directory")
        _protected(asset, private=name == "tls_key")
        files.append(asset)
    return SmokeConfig(directory, box, principal, float(start), duration, files[0], files[1], files[2], port)


def _write_once(path: Path, data: dict[str, Any]) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def calendar(config: SmokeConfig) -> Iterator[None]:
    verifier_module = vars(night_verifier)
    original_night, original_verifier = night.window_for, verifier_module["window_for"]
    night.window_for = config.window
    verifier_module["window_for"] = config.window
    try:
        yield
    finally:
        night.window_for = original_night
        verifier_module["window_for"] = original_verifier


def smoke_app(config: SmokeConfig) -> FastAPI:
    if config.window(time.time()) is None:
        raise ValueError("fixture_window_expired_or_not_started")
    if (config.directory / "board.sqlite3").exists():
        raise ValueError("fixture_database_already_exists")
    line = config.verifier_public_key.read_text().strip()
    parts = line.split()
    if len(parts) != 4 or parts[:3] != [f"verifier@{config.box}", 'namespaces="switchboard"', "ssh-ed25519"]:
        raise ValueError("invalid_fixture_public_key")
    blob = base64.b64decode(parts[3], validate=True)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    roles = config.directory / "verifiers.json"
    _write_once(roles, {f"verifier@{config.box}": {"box": config.box, "fingerprint": fingerprint}})
    settings = Settings.from_env(
        {
            "BOARD_STATE_DIR": str(config.directory),
            "BOARD_HUMAN_PASSWORD_HASH": passwords.hash_password(secrets.token_urlsafe(32)),
            "BOARD_PUBLIC_URL": f"https://127.0.0.1:{config.port}",
            "BOARD_DIST_DIR": str(config.directory / "empty-dist"),
            "BOARD_UPDATE_STATUS": str(config.directory / "no-update-status.json"),
            "BOARD_NIGHT_VERIFIERS_FILE": str(roles),
            "BOARD_SSH_KEYGEN": "/usr/bin/ssh-keygen",
            "BOARD_LISTEN": f"127.0.0.1:{config.port}",
            "BOARD_SIGNING_NAMESPACE": "switchboard",
        }
    )
    app = create_app(settings)
    app.state.registry.add_line(line)
    return app


async def admit(config: SmokeConfig) -> None:
    if config.window(time.time()) is None:
        raise ValueError("fixture_window_expired_or_not_started")
    path = config.directory / "board.sqlite3"
    _protected(path, private=True)
    db = Database(path)

    def ready(conn: Any) -> bool:
        identity = conn.execute(
            "SELECT journal_id, counter FROM night_verifier_state WHERE box = ?", (config.box,)
        ).fetchone()
        return (
            identity is not None
            and identity["counter"] == 0
            and conn.execute("SELECT COUNT(*) FROM night_runs").fetchone()[0] == 0
        )

    if not await db.read(ready):
        raise ValueError("fixture_not_bootstrapped_or_already_admitted")
    admission = night.NightAdmission(db)
    await admission.set_enabled(True)
    await admission.enroll(config.principal, True)
    run = await admission.admit(config.principal, time.time())
    _write_once(
        config.directory / "run.json",
        {"run_id": run.id, "fence": run.fence, "board_url": f"https://127.0.0.1:{config.port}"},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "admit"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("root-controlled smoke fixture required")
    os.umask(0o077)
    config = load(args.config)
    with calendar(config):
        if args.command == "admit":
            asyncio.run(admit(config))
        else:
            app = smoke_app(config)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", config.port))
                listener.listen(16)
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        ssl_certfile=str(config.tls_cert),
                        ssl_keyfile=str(config.tls_key),
                        access_log=False,
                    )
                )
                asyncio.run(server.serve(sockets=[listener]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
