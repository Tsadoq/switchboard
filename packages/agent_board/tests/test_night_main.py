import io
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board.night import __main__ as entry
from agent_board.night import client
from agent_board.night.gate import GuardState


def test_every_command_refuses_to_run_without_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entry, "_is_root", lambda: False)
    touched: list[Path] = []
    monkeypatch.setattr(entry, "load", lambda path: touched.append(path))

    for command in ("serve", "guard", "gate", "exec"):
        assert entry.main([command, "--config", str(tmp_path / "verifier.toml")]) == 2

    assert touched == [], "the config was read before the root check"


def test_the_guard_state_comes_from_the_system_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="ActiveState=active\nInvocationID=" + "a" * 32 + "\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    state = entry._guard_state("switchboard-night-guard-smoke.service")

    assert state == GuardState(True, "a" * 32), state
    assert seen[0][:4] == [
        "/usr/bin/systemctl",
        "--system",
        "show",
        "switchboard-night-guard-smoke.service",
    ], seen


def test_an_unreadable_guard_counts_as_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", failing)

    assert entry._guard_state("switchboard-night-guard.service") == GuardState(False, "")


def test_the_entry_point_does_not_import_the_ordinary_runner() -> None:
    probe = "import sys; import agent_board.night.__main__; assert 'agent_board.runner' not in sys.modules"

    subprocess.run([sys.executable, "-I", "-c", probe], check=True, capture_output=True, timeout=10)


def _verifier(path: Path, reply: bytes) -> tuple[socket.socket, list[bytes]]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    received: list[bytes] = []

    def answer() -> None:
        connection, _ = listener.accept()
        with connection:
            received.append(connection.recv(9000))
            connection.sendall(reply)

    threading.Thread(target=answer, daemon=True).start()
    return listener, received


def test_request_sends_one_frame_as_the_caller_with_the_fence_from_stdin(tmp_path: Path) -> None:
    path = tmp_path / "verifier.sock"
    run_id, fence = str(uuid4()), "f" * 43
    listener, received = _verifier(path, b'{"v":1,"status":"running"}\n')
    out = io.StringIO()

    with listener:
        code = client.main(
            ["start", run_id, "--socket", str(path)], stdin=io.StringIO(fence + "\n"), stdout=out
        )

    frame = json.loads(received[0])
    assert code == 0 and json.loads(out.getvalue())["status"] == "running"
    assert frame == {**frame, "v": 1, "op": "start", "run_id": run_id, "fence": fence}, frame
    assert received[0].endswith(b"\n") and received[0].count(b"\n") == 1


def test_a_refused_answer_exits_nonzero(tmp_path: Path) -> None:
    path = tmp_path / "verifier.sock"
    listener, _ = _verifier(path, b'{"v":1,"status":"refused","reason":"capacity"}\n')

    with listener:
        code = client.main(
            ["status", str(uuid4()), "--socket", str(path)],
            stdin=io.StringIO("f" * 43 + "\n"),
            stdout=io.StringIO(),
        )

    assert code == client.REFUSED_EXIT


def test_request_runs_without_root_and_never_takes_the_fence_on_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    seen: list[list[str]] = []

    def fake_client(argv: list[str]) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(client, "main", fake_client)

    assert entry.main(["request", "stop", str(uuid4()), "--socket", "/run/x.sock"]) == 0
    assert seen and len(seen[0]) == 4, "request parsing accepted extra argv, such as a fence"


def test_the_client_runs_as_its_own_module_without_root_code() -> None:
    probe = (
        "import sys; import agent_board.night.client; "
        "assert 'agent_board.night.controller' not in sys.modules; "
        "assert 'agent_board.night_state' not in sys.modules"
    )

    subprocess.run([sys.executable, "-I", "-c", probe], check=True, capture_output=True, timeout=10)
    usage = subprocess.run(
        [sys.executable, "-I", "-m", "agent_board.night.client", "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "--socket" in usage.stdout
