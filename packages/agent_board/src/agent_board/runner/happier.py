import json
import logging
import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_NOT_FOUND = "session_not_found"
SESSION_ARCHIVED = "session_archived"
_SESSION_GONE = (SESSION_NOT_FOUND, SESSION_ARCHIVED)
HOME_DIR_ENV = "HAPPIER_HOME_DIR"
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
    def __init__(self, bin: str = "happier", session_home: Path | None = None) -> None:
        self._bin = bin
        self._session_env = None if session_home is None else {**os.environ, HOME_DIR_ENV: str(session_home)}

    def create(
        self,
        *,
        backend: str,
        path: Path,
        tag: str,
        title: str,
        prompt: str,
        permission_mode: str,
        model: str | None = None,
        terminal_json: str | None = None,
        env: Mapping[str, str] | None = None,
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
            *_model_flag(model),
            *(() if terminal_json is None else ("--terminal-json", terminal_json)),
            *(part for name, value in sorted((env or {}).items()) for part in ("--env", f"{name}={value}")),
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

    def send(self, session_id: str, message: str, model: str | None = None) -> None:
        self._envelope("session", "send", session_id, _Body(message), *_model_flag(model), "--json")

    def archive(self, session_id: str) -> None:
        try:
            self._envelope("session", "archive", session_id, "--json")
        except HappierError as error:
            if error.code not in _SESSION_GONE:
                raise

    def idle(self, session_id: str, timeout: int = 3) -> bool:
        """`status` cannot see a running turn (probe 5), so only `wait` can answer this."""
        try:
            data = self._envelope("session", "wait", session_id, "--timeout", str(timeout), "--json")
        except HappierError as error:
            if error.code == "timeout":
                return False
            if error.code in _SESSION_GONE:
                return True
            raise
        return isinstance(data, dict) and data.get("idle") is True

    def notify(self, title: str, message: str) -> None:
        completed = self._run("notify", "-p", _Body(message), "-t", title)
        if completed.returncode != 0:
            raise HappierError(f"exit_{completed.returncode}")

    def _envelope(self, *args: str | _Body) -> Any:
        completed = self._run(*args, env=self._session_env)
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

    def _run(self, *args: str | _Body, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        argv = [self._bin, *(arg.text if isinstance(arg, _Body) else arg for arg in args)]
        shown = shlex.join([self._bin, *(_REDACTED if isinstance(arg, _Body) else arg for arg in args)])
        logger.info("running %s", shown)
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False, env=env
            )
        except FileNotFoundError:
            raise HappierError("not_installed") from None
        except subprocess.TimeoutExpired:
            raise HappierError("timeout") from None
        if completed.returncode != 0:
            logger.warning("%s exited %d: %s", shown, completed.returncode, completed.stderr.strip())
        return completed


def _model_flag(model: str | None) -> tuple[str, ...]:
    return () if model is None else ("--model", model)
