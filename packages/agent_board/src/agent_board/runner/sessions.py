import fcntl
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_board.cursors import CONSUMER_ENV, CURSOR_DIR, CursorError, hand_off
from agent_board.identity import Identity, ensure_state_dir
from agent_board.runner import remote_control
from agent_board.runner.config import REMOTE_CONTROL, KindConfig
from agent_board.runner.happier import Happier, HappierError, Session

NOTIFY_TITLE = "agent board"

MAP_FILE = "runner-sessions.json"
LOCK_FILE = "runner-sessions.lock"
CONSUMERS_FILE = "runner-consumers.json"
LEGACY_FILE = "runner-session.json"
KIND_KEY = "kind"
TOPIC_PREFIX = "topic:"
RETIRE_MIN_AGE_SECONDS = 90
REBUILD_CONTEXT = (
    "This is a new session for thread {thread}. The board is your memory, not this conversation: "
    "before acting, run `agent-board read {thread} --last 30` (the whole thread only if those are not enough), "
    "`agent-board memory show`, and `agent-board tickets list --mine`."
)
_THREAD_SCOPE = "thread"
_MAP_VERSION = 1
_EVICTION_CANDIDATES = 2
_RETIRED_SUFFIX = ".retired"
_CORRUPT_SUFFIX = ".corrupt-"
_ONE_SHOT_LABEL = "one-shot"
_RECORDED_ID = re.compile(r'"session_id"\s*:\s*"([^"]+)"')
logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    key: str | None
    session_id: str
    last_delivery: float
    retire_when_idle: bool
    consumer: str | None = None


class SessionPool:
    """The Happier sessions one kind is woken into.

    A key names the conversation a wake belongs to: `KIND_KEY` (the one session of kind scope),
    `topic_key(n)` (thread scope), or None (a one-shot session, archived once idle). A call may wait for
    `agent-board wake-test`, which records sessions from another process.
    """

    def __init__(
        self, settings: KindConfig, me: Identity, happier: Happier, clock: Callable[[], float] = time.time
    ) -> None:
        self._settings = settings
        self._me = me
        self._happier = happier
        self._clock = clock
        self._thread_scope = settings.session_scope == _THREAD_SCOPE

    def deliver(
        self,
        key: str | None,
        prompt: str | Callable[[bool], str],
        model: str | None,
        deadline: float,
        on_sent: Callable[[str], None] | None = None,
    ) -> bool:
        """Send `prompt` into the session for `key`, opening one if needed, and pass its id to `on_sent`.

        A callable prompt is told whether the receiving session has a read cursor: an existing session only if the
        runner recorded a consumer for it, a newly opened one always.

        False means the wakes should stay leased: the session is busy, the cap is reached with no idle
        session to evict, or evicting one ran past `deadline` (on the `clock` timebase).
        """
        with self._map() as entries:
            current = _find(entries, key) if key is not None else None
            if current is not None:
                state = self._happier.status(current.session_id)
                if state is not None and state.active:
                    if state.busy:
                        logger.info(
                            "%s: session %s is busy; wakes left leased for redelivery",
                            self._me.principal,
                            current.session_id,
                        )
                        return False
                    self._happier.send(current.session_id, _text(prompt, current.consumer is not None), model)
                    current.last_delivery = self._clock()
                    if on_sent is not None:
                        on_sent(current.session_id)
                    logger.info(
                        "%s: sent wakes to session %s for %s (model %s)",
                        self._me.principal,
                        current.session_id,
                        key,
                        model or "default",
                    )
                    return True
                entries.remove(current)
            if self._thread_scope and len(entries) >= self._settings.max_sessions:
                if not self._evict_one(entries):
                    logger.info(
                        "%s: %d sessions live and none idle; wakes left leased for redelivery",
                        self._me.principal,
                        len(entries),
                    )
                    return False
                if self._clock() > deadline:
                    return False
            opened = self._open_or_fall_back(entries, key, prompt, model)
            if opened is not None and on_sent is not None:
                on_sent(opened.id)
            return True

    def housekeep(self) -> None:
        legacy = ensure_state_dir(self._me.kind) / LEGACY_FILE
        with self._map() as entries:
            self._adopt_legacy(entries, legacy)
            for entry in list(entries):
                if not self._scope_produces(entry.key):
                    entry.retire_when_idle = True
                state = self._happier.status(entry.session_id)
                if state is None or not state.active:
                    entries.remove(entry)
                    continue
                why = self._reap_reason(entry)
                if why is not None and self._idle(entry):
                    self._archive(entries, entry, why)
        if legacy.exists():
            legacy.replace(legacy.with_name(LEGACY_FILE + _RETIRED_SUFFIX))

    def open_for_wake_test(self, prompt: str) -> Session:
        """In kind scope the new session takes over `KIND_KEY`; the one it displaces retires once idle."""
        with self._map() as entries:
            key = None
            if not self._thread_scope:
                key = KIND_KEY
                if (previous := _find(entries, KIND_KEY)) is not None:
                    previous.key = None
                    previous.retire_when_idle = True
            return self._open(entries, key, prompt, None)

    def _open_or_fall_back(
        self, entries: list[_Entry], key: str | None, prompt: str | Callable[[bool], str], model: str | None
    ) -> Session | None:
        """A new runner session gets a consumer, so it is offered --new; a Remote Control fallback has none."""
        try:
            return self._open(entries, key, _with_rebuild_context(key, _text(prompt, True)), model)
        except HappierError as error:
            fallback_allowed = model is None and self._settings.fallback == REMOTE_CONTROL
            if not fallback_allowed or not remote_control.serves(self._me.kind):
                raise
            logger.warning(
                "%s: create failed (%s); falling back to remote control", self._me.principal, error
            )
            remote_control.start(
                self._settings.workdir,
                f"board-{self._me.kind}",
                _with_rebuild_context(key, _text(prompt, False)),
            )
            return None

    def _open(self, entries: list[_Entry], key: str | None, prompt: str, model: str | None) -> Session:
        consumer = uuid4().hex
        self._inherit(key, consumer)
        session = self._happier.create(
            backend=self._settings.backend,
            path=self._settings.workdir,
            tag=f"board-{self._me.kind}",
            title=f"agent board: {self._me.principal}",
            prompt=prompt,
            permission_mode=self._settings.permission_mode,
            model=model,
            env={CONSUMER_ENV: consumer},
        )
        entries.append(
            _Entry(key, session.id, self._clock(), retire_when_idle=key is None, consumer=consumer)
        )
        if key is not None:
            self._remember(key, consumer)
        logger.info("%s: opened session %s for %s", self._me.principal, session.id, _label(key))
        return session

    def _inherit(self, key: str | None, consumer: str) -> None:
        """A new session for a key takes over its predecessor's read positions; the predecessor is retired first."""
        previous = None if key is None else self._consumers().get(key)
        if previous is None:
            return
        try:
            copied = hand_off(ensure_state_dir(self._me.kind) / CURSOR_DIR, previous, consumer)
        except CursorError as error:
            logger.warning("%s: read cursors not handed over for %s: %s", self._me.principal, key, error)
            return
        logger.info(
            "%s: handed read cursors of %s to %s for %s (%d threads)",
            self._me.principal,
            previous,
            consumer,
            key,
            len(copied),
        )

    def _consumers(self) -> dict[str, str]:
        try:
            recorded = json.loads((ensure_state_dir(self._me.kind) / CONSUMERS_FILE).read_text())
        except (FileNotFoundError, ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(recorded, dict):
            return {}
        return {
            key: value for key, value in recorded.items() if isinstance(key, str) and isinstance(value, str)
        }

    def _remember(self, key: str, consumer: str) -> None:
        consumers = self._consumers() | {key: consumer}
        _replace_atomically(
            ensure_state_dir(self._me.kind) / CONSUMERS_FILE, json.dumps(consumers, sort_keys=True)
        )

    def _evict_one(self, entries: list[_Entry]) -> bool:
        by_age = sorted(entries, key=lambda entry: entry.last_delivery)
        for candidate in by_age[:_EVICTION_CANDIDATES]:
            if self._idle(candidate):
                self._archive(entries, candidate, "evicted at cap")
                return True
        return False

    def _reap_reason(self, entry: _Entry) -> str | None:
        if entry.retire_when_idle:
            return "retire when idle"
        if self._thread_scope and self._clock() - entry.last_delivery >= self._settings.idle_ttl_seconds:
            return "idle ttl"
        return None

    def _idle(self, entry: _Entry) -> bool:
        """Decision 7: a just-sent prompt may not have started its turn yet, so `wait` alone can lie."""
        old_enough = self._clock() - entry.last_delivery >= RETIRE_MIN_AGE_SECONDS
        return old_enough and self._happier.idle(entry.session_id)

    def _archive(self, entries: list[_Entry], entry: _Entry, why: str) -> None:
        self._happier.archive(entry.session_id)
        entries.remove(entry)
        logger.info(
            "%s: archived session %s for %s (%s)",
            self._me.principal,
            entry.session_id,
            _label(entry.key),
            why,
        )

    def _scope_produces(self, key: str | None) -> bool:
        if self._thread_scope:
            return key is None or key.startswith(TOPIC_PREFIX)
        return key == KIND_KEY

    def _adopt_legacy(self, entries: list[_Entry], legacy: Path) -> None:
        try:
            text = legacy.read_text()
            adopted_at = legacy.stat().st_mtime
        except FileNotFoundError:
            return
        session_id = _legacy_session_id(text)
        if session_id is None or _find_session(entries, session_id) is not None:
            return
        key = KIND_KEY if _find(entries, KIND_KEY) is None else None
        entries.append(_Entry(key, session_id, adopted_at, retire_when_idle=key is None))
        if self._thread_scope:
            logger.info("%s: retired legacy session %s", self._me.principal, session_id)
        else:
            logger.info("%s: adopted legacy session %s for %s", self._me.principal, session_id, _label(key))

    @contextmanager
    def _map(self) -> Iterator[list[_Entry]]:
        directory = ensure_state_dir(self._me.kind)
        with open(directory / LOCK_FILE, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            entries = self._load(directory / MAP_FILE)
            loaded = _dump(entries)
            try:
                yield entries
            finally:
                if (current := _dump(entries)) != loaded:
                    _replace_atomically(directory / MAP_FILE, current)

    def _load(self, path: Path) -> list[_Entry]:
        try:
            text = path.read_text()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError):
            text = None
        entries = None if text is None else _parse(text)
        if entries is not None:
            return entries
        self._set_aside(path, text or "")
        return []

    def _set_aside(self, path: Path, text: str) -> None:
        set_aside = path.with_name(f"{path.name}{_CORRUPT_SUFFIX}{int(self._clock())}")
        path.replace(set_aside)
        lost = _RECORDED_ID.findall(text)
        logger.warning(
            "%s: %s is unreadable; moved it to %s and starting with no sessions (it named: %s)",
            self._me.principal,
            path,
            set_aside.name,
            ", ".join(lost) or "no session ids",
        )
        warn_human(
            self._happier,
            f"{self._me.principal}: the session map {path} was unreadable and was moved to {set_aside.name}; "
            "sessions it recorded are no longer tracked",
        )


def _text(prompt: str | Callable[[bool], str], cursor: bool) -> str:
    return prompt if isinstance(prompt, str) else prompt(cursor)


def topic_key(topic: int | str) -> str:
    return f"{TOPIC_PREFIX}{topic}"


def warn_human(happier: Happier, message: str) -> None:
    logger.warning("%s", message)
    try:
        happier.notify(NOTIFY_TITLE, message)
    except HappierError as error:
        logger.warning("could not notify the human: %s", error)


def _with_rebuild_context(key: str | None, prompt: str) -> str:
    if key is None or not key.startswith(TOPIC_PREFIX):
        return prompt
    return f"{prompt} {REBUILD_CONTEXT.format(thread=key.removeprefix(TOPIC_PREFIX))}"


def _label(key: str | None) -> str:
    return _ONE_SHOT_LABEL if key is None else key


def _find(entries: list[_Entry], key: str) -> _Entry | None:
    return next((entry for entry in entries if entry.key == key), None)


def _find_session(entries: list[_Entry], session_id: str) -> _Entry | None:
    return next((entry for entry in entries if entry.session_id == session_id), None)


def _legacy_session_id(text: str) -> str | None:
    try:
        session_id = json.loads(text)["session_id"]
    except (ValueError, KeyError, TypeError):
        return None
    return session_id if isinstance(session_id, str) and session_id else None


def _parse(text: str) -> list[_Entry] | None:
    try:
        recorded = json.loads(text)
    except ValueError:
        return None
    if not isinstance(recorded, dict) or recorded.get("v") != _MAP_VERSION:
        return None
    sessions = recorded.get("sessions")
    if not isinstance(sessions, list):
        return None
    entries = [_entry(session) for session in sessions]
    return None if None in entries else [entry for entry in entries if entry is not None]


def _entry(recorded: Any) -> _Entry | None:
    match recorded:
        case {
            "key": str() | None as key,
            "session_id": str() as session_id,
            "last_delivery": int() | float() as last_delivery,
            "retire_when_idle": bool() as retire_when_idle,
        } if session_id and not isinstance(last_delivery, bool):
            consumer = recorded.get("consumer")
            return _Entry(
                key,
                session_id,
                float(last_delivery),
                retire_when_idle,
                consumer if isinstance(consumer, str) else None,
            )
    return None


def _dump(entries: list[_Entry]) -> str:
    return json.dumps({"v": _MAP_VERSION, "sessions": [asdict(entry) for entry in entries]})


def _replace_atomically(path: Path, text: str) -> None:
    staged = path.with_name(f"{path.name}.tmp")
    staged.write_text(text)
    os.replace(staged, path)
