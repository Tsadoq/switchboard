import json
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_NOT_FOUND = "session_not_found"
TIMEOUT_SECONDS = 60
_REDACTED = "<redacted>"
logger = logging.getLogger(__name__)


class HappierError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Session:
    id: str
    created: bool


@dataclass(frozen=True)
class SessionState:
    active: bool
    pending_requests: int
    controlled_by_user: bool

    @property
    def busy(self) -> bool:
        return self.pending_requests > 0 or self.controlled_by_user


@dataclass(frozen=True)
class _Body:
    text: str


class Happier:
    def __init__(self, bin: str = "happier") -> None:
        self._bin = bin

    def create(
        self, *, backend: str, path: Path, tag: str, title: str, prompt: str, permission_mode: str
    ) -> Session:
        data = self._envelope(
            "session",
            "create",
            "--backend",
            backend,
            "--path",
            str(path),
            "--tag",
            tag,
            "--title",
            title,
            "--prompt",
            _Body(prompt),
            "--permission-mode",
            permission_mode,
            "--json",
        )
        try:
            return Session(str(data["session"]["id"]), bool(data["created"]))
        except (KeyError, TypeError):
            raise HappierError("bad_output") from None

    def status(self, session_id: str) -> SessionState | None:
        try:
            data = self._envelope("session", "status", session_id, "--live", "--json")
        except HappierError as error:
            if error.code == SESSION_NOT_FOUND:
                return None
            raise
        try:
            agent_state = data.get("agentState") or {}
            return SessionState(
                active=bool(data["session"]["active"]),
                pending_requests=int(agent_state.get("pendingRequestsCount", 0)),
                controlled_by_user=bool(agent_state.get("controlledByUser", False)),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise HappierError("bad_output") from None

    def send(self, session_id: str, message: str) -> None:
        self._envelope("session", "send", session_id, _Body(message), "--json")

    def notify(self, title: str, message: str) -> None:
        completed = self._run("notify", "-p", _Body(message), "-t", title)
        if completed.returncode != 0:
            raise HappierError(f"exit_{completed.returncode}")

    def _envelope(self, *args: str | _Body) -> Any:
        completed = self._run(*args)
        try:
            envelope = json.loads(completed.stdout)
        except ValueError:
            envelope = None
        if not isinstance(envelope, dict):
            raise HappierError(f"exit_{completed.returncode}" if completed.returncode else "bad_output")
        if envelope.get("ok") is True:
            return envelope.get("data")
        error = envelope.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise HappierError(str(code or f"exit_{completed.returncode}"))

    def _run(self, *args: str | _Body) -> subprocess.CompletedProcess[str]:
        argv = [self._bin, *(arg.text if isinstance(arg, _Body) else arg for arg in args)]
        shown = shlex.join([self._bin, *(_REDACTED if isinstance(arg, _Body) else arg for arg in args)])
        logger.info("running %s", shown)
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
            )
        except FileNotFoundError:
            raise HappierError("not_installed") from None
        except subprocess.TimeoutExpired:
            raise HappierError("timeout") from None
        if completed.returncode != 0:
            logger.warning("%s exited %d: %s", shown, completed.returncode, completed.stderr.strip())
        return completed
