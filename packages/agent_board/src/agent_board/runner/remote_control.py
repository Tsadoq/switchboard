import re
import subprocess
import time
from pathlib import Path

PROMPT_WAIT_SECONDS = 60
_SERVED_KIND = "claude"
_TMUX_TIMEOUT_SECONDS = 10
_PROMPT_POLL_SECONDS = 0.5
_PROMPT_BOX = re.compile(r"^[\s│]*[>❯](\s|$)", re.MULTILINE)


class RemoteControlError(Exception):
    pass


def serves(kind: str) -> bool:
    return kind == _SERVED_KIND


def start(workdir: Path, name: str, prompt: str) -> None:
    if not _session_exists(name):
        _tmux("new-session", "-d", "-s", name, "-c", str(workdir), "claude", "remote-control", "--name", name)
    _await_prompt_box(name)
    _tmux("load-buffer", "-b", name, "-", stdin=prompt)
    _tmux("paste-buffer", "-d", "-p", "-b", name, "-t", name)
    _tmux("send-keys", "-t", name, "Enter")


def _session_exists(name: str) -> bool:
    return _run("has-session", "-t", name).returncode == 0


def _await_prompt_box(name: str) -> None:
    deadline = time.monotonic() + PROMPT_WAIT_SECONDS
    while not _PROMPT_BOX.search(_tmux("capture-pane", "-p", "-t", name)):
        if time.monotonic() > deadline:
            raise RemoteControlError(
                f"claude remote-control in tmux session {name} showed no prompt box within "
                f"{PROMPT_WAIT_SECONDS}s; it may be waiting on a trust or enable question"
            )
        time.sleep(_PROMPT_POLL_SECONDS)


def _tmux(*args: str, stdin: str | None = None) -> str:
    completed = _run(*args, stdin=stdin)
    if completed.returncode != 0:
        raise RemoteControlError(f"tmux {args[0]} exited {completed.returncode}: {completed.stderr.strip()}")
    return completed.stdout


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["tmux", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise RemoteControlError("tmux is not on PATH") from None
    except subprocess.TimeoutExpired:
        raise RemoteControlError(f"tmux {args[0]} did not finish in {_TMUX_TIMEOUT_SECONDS}s") from None
