import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest
import uvicorn
from board.app import create_app
from board.constants import DEFAULT_NAMESPACE as NAMESPACE
from board.identity.registry import Registry
from board.settings import Settings

CLAUDE_AI_AGENT = "claude-code_test"
FAKE_BIN = Path(__file__).parent / "fake_bin"
AGENT_BOARD = Path(sys.executable).parent / "agent-board"
_AI_AGENT_OF_KIND = {"claude": "claude-code"}
_SERVER_START_SECONDS = 15


class AgentBoard(Protocol):
    def __call__(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]: ...


class FakeSign:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._root = root
        self._monkeypatch = monkeypatch

    def configure(self, box: str, kinds: list[str]) -> dict[str, str]:
        self._root.mkdir(exist_ok=True)
        conf = self._root / "agent-identity.conf"
        conf.write_text(
            json.dumps(
                {
                    "box": box,
                    "identity_dir": ".agents/identity",
                    "kinds": [
                        {"ai_agent": _AI_AGENT_OF_KIND.get(kind, kind), "name": kind} for kind in kinds
                    ],
                    "namespace": NAMESPACE,
                }
            )
        )
        key_dir = self._root / "keys"
        self._monkeypatch.setenv("AGENT_IDENTITY_CONF", str(conf))
        self._monkeypatch.setenv("FAKE_SIGN_KEY_DIR", str(key_dir))
        return {kind: self._generate_key(key_dir / kind, f"{kind}@{box}") for kind in kinds}

    def _generate_key(self, kind_dir: Path, principal: str) -> str:
        kind_dir.mkdir(parents=True, exist_ok=True)
        key = kind_dir / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", principal, "-f", str(key)],
            check=True,
            timeout=5,
        )
        key_type, blob = Path(f"{key}.pub").read_text().split()[:2]
        return f'{principal} namespaces="{NAMESPACE}" {key_type} {blob}'


@dataclass(frozen=True)
class BoardServer:
    url: str
    registry: Registry


@pytest.fixture
def claude_ai_agent() -> str:
    return CLAUDE_AI_AGENT


@pytest.fixture
def fake_sign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSign:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}")
    for inherited in ("AI_AGENT", "AGENT_BOARD_URL", "AGENT_IDENTITY_CONF"):
        monkeypatch.delenv(inherited, raising=False)
    return FakeSign(tmp_path / "fake-sign", monkeypatch)


@pytest.fixture
def agent_board(fake_sign: FakeSign) -> AgentBoard:
    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(AGENT_BOARD), *args], input=stdin, capture_output=True, text=True, timeout=60, check=False
        )

    return run


@pytest.fixture
def board_server(tmp_path: Path) -> Iterator[BoardServer]:
    app = create_app(Settings.from_env({"BOARD_STATE_DIR": str(tmp_path / "board-state")}))
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + _SERVER_START_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("board server did not start")
        time.sleep(0.05)
    try:
        yield BoardServer(f"http://127.0.0.1:{port}", app.state.registry)
    finally:
        server.should_exit = True
        thread.join(timeout=_SERVER_START_SECONDS)
        listener.close()
