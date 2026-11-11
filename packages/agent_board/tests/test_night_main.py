import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from agent_board.night import __main__ as entry
from agent_board.night.gate import GuardState


def test_every_command_refuses_to_run_without_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    touched: list[Path] = []
    monkeypatch.setattr(entry, "load", lambda path: touched.append(path))

    for command in ("serve", "guard", "gate"):
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

    state = entry._guard_state()

    assert state == GuardState(True, "a" * 32), state
    assert seen[0][:4] == ["/usr/bin/systemctl", "--system", "show", "switchboard-night-guard.service"], seen


def test_an_unreadable_guard_counts_as_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", failing)

    assert entry._guard_state() == GuardState(False, "")


def test_the_entry_point_does_not_import_the_ordinary_runner() -> None:
    probe = "import sys; import agent_board.night.__main__; assert 'agent_board.runner' not in sys.modules"

    subprocess.run([sys.executable, "-I", "-c", probe], check=True, capture_output=True, timeout=10)
