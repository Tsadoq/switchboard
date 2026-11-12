import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest


def _children(pid: int) -> list[int]:
    found: list[int] = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        text = (task / "children").read_text().split()
        found.extend(int(child) for child in text)
    return found


def _session(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[3])


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_cleanup_probe_handles_a_disappearing_process(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    def vanished(*args: object, **kwargs: object) -> str:
        raise error("process disappeared")

    monkeypatch.setattr(Path, "read_text", vanished)
    assert not _alive(123)


def test_cleanup_probe_does_not_hide_permission_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> str:
        raise PermissionError("not a disappearance")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        _alive(123)


def test_the_stub_ignores_sigterm_and_leaves_a_setsid_child() -> None:
    stub = subprocess.Popen(
        [sys.executable, "-I", "-m", "agent_board.night.acceptance_stub", str(uuid4())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    children: list[int] = []
    try:
        deadline = time.monotonic() + 10
        while not children and time.monotonic() < deadline:
            time.sleep(0.05)
            children = _children(stub.pid)
        assert len(children) == 1, f"expected one forked child, found {children}"
        child = children[0]
        deadline = time.monotonic() + 10
        while _session(child) != child and time.monotonic() < deadline:
            time.sleep(0.05)

        stub.send_signal(signal.SIGTERM)
        time.sleep(0.3)

        assert stub.poll() is None, "the stub exited on SIGTERM"
        assert _session(child) == child, "the child did not start its own session"
        assert _session(child) != _session(stub.pid)
    finally:
        for pid in (*children, stub.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stub.wait(timeout=10)
    for child in children:
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)


def test_the_stub_refuses_unexpected_arguments() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "agent_board.night.acceptance_stub"],
        check=False,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 2
