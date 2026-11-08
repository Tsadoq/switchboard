import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

type Request = Literal["check", "apply"]

REQUESTS: tuple[Request, ...] = ("apply", "check")
_SHA = re.compile(r"[0-9a-f]{40}")
_BUSY_STATES = frozenset({"checking", "applying"})


class Unavailable(Exception):
    pass


class NotOffered(Exception):
    pass


@dataclass(frozen=True)
class Commit:
    commit: str
    short: str
    subject: str
    date: str

    @classmethod
    def parse(cls, raw: object) -> "Commit | None":
        if not isinstance(raw, dict) or not _SHA.fullmatch(str(raw.get("commit", ""))):
            return None
        commit = str(raw["commit"])
        return cls(
            commit=commit,
            short=commit[:12],
            subject=str(raw.get("subject", "")),
            date=str(raw.get("date", "")),
        )


@dataclass(frozen=True)
class LastUpdate:
    ok: bool
    to: str
    rolled_back: bool
    finished_at: float | None
    message: str

    @classmethod
    def parse(cls, raw: object) -> "LastUpdate | None":
        if not isinstance(raw, dict):
            return None
        return cls(
            ok=raw.get("ok") is True,
            to=str(raw.get("to", ""))[:12],
            rolled_back=raw.get("rolled_back") is True,
            finished_at=_number(raw.get("finished_at")),
            message=str(raw.get("message", "")),
        )


@dataclass(frozen=True)
class Status:
    state: str
    remote: str
    branch: str
    checked_at: float | None
    installed: Commit | None
    latest: Commit | None
    behind: int | None
    pending: list[Commit]
    last_update: LastUpdate | None
    error: str | None
    requested: str | None
    log_tail: list[str]
    queued: Request | None = None
    offered: Commit | None = field(init=False)

    def __post_init__(self) -> None:
        offered = self.latest if self.behind and self.installed else None
        object.__setattr__(self, "offered", offered)

    @property
    def busy(self) -> bool:
        return self.queued is not None or self.state in _BUSY_STATES

    @classmethod
    def parse(cls, raw: dict[str, Any], queued: Request | None) -> "Status":
        pending = raw.get("pending")
        behind = raw.get("behind")
        error = raw.get("error")
        requested = raw.get("requested")
        log_tail = raw.get("log_tail")
        return cls(
            state=str(raw.get("state", "idle")),
            remote=str(raw.get("remote", "")),
            branch=str(raw.get("branch", "")),
            checked_at=_number(raw.get("checked_at")),
            installed=Commit.parse(raw.get("installed")),
            latest=Commit.parse(raw.get("latest")),
            behind=behind if isinstance(behind, int) and not isinstance(behind, bool) else None,
            pending=[c for c in map(Commit.parse, pending if isinstance(pending, list) else []) if c],
            last_update=LastUpdate.parse(raw.get("last_update")),
            error=str(error) if error else None,
            requested=str(requested)[:12] if requested else None,
            log_tail=[str(line) for line in log_tail] if isinstance(log_tail, list) else [],
            queued=queued,
        )


class Updates:
    def __init__(self, status_path: Path, request_dir: Path) -> None:
        self._status_path = status_path
        self._request_dir = request_dir

    def status(self) -> Status | None:
        try:
            raw = json.loads(self._status_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        return Status.parse(raw, self._queued())

    def waiting(self) -> int:
        status = self.status()
        return (status.behind or 0) if status else 0

    def request_check(self) -> None:
        if self.status() is None:
            raise Unavailable
        self._request("check", "")

    def request_apply(self, commit: str) -> None:
        status = self.status()
        if status is None:
            raise Unavailable
        if status.offered is None or status.offered.commit != commit or status.busy:
            raise NotOffered
        self._request("apply", commit)

    def _queued(self) -> Request | None:
        return next((name for name in REQUESTS if (self._request_dir / name).exists()), None)

    def _request(self, name: Request, content: str) -> None:
        self._request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._request_dir / f".{name}.tmp"
        temporary.write_text(content + "\n")
        os.replace(temporary, self._request_dir / name)


def _number(raw: object) -> float | None:
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    return None
