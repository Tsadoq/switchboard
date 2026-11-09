import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal, NoReturn

from agent_board.client import BoardError, BoardUnreachable, Client
from agent_board.identity import Identity, ensure_state_dir, state_dir
from agent_board.runner import remote_control
from agent_board.runner.config import REMOTE_CONTROL, KindConfig, RunnerConfig
from agent_board.runner.happier import TIMEOUT_SECONDS, Happier, HappierError, Session
from agent_board.runner.remote_control import RemoteControlError
from agent_board.runner.weekly import WeeklyClock

SESSION_FILE = "runner-session.json"
NOTIFY_TITLE = "agent board"
MY_INBOX: Final = "me"
HUMAN_INBOX: Final = "human"
TIMER_REASON: Final = "timer"
WEEKLY_REASON: Final = "weekly"
WEEKLY_FILE = "runner-weekly.json"
_MODEL_PRIORITY = (WEEKLY_REASON, "mention", "follow", TIMER_REASON)
_ALREADY_ACKED = (
    "The runner has already acknowledged these wakes, so `agent-board inbox` will not list them "
    "and there is nothing to ack."
)
_ANSWER_THREADS = (
    "Read each thread named above with `agent-board read <id>`. Reply with "
    '`agent-board reply <id> "..."` only when a post asks you something, needs action from you, '
    "or you have something new to add; a post that only informs needs no reply. "
    "Always reply to a post by the human that responds to you, even if only to confirm."
)
_CHECK_TICKETS = "For the scheduled wake, check your tickets with `agent-board tickets list --mine`."
_WEEKLY_SUMMARY = "the weekly scheduled review"
_WEEKLY_REVIEW = (
    "For the weekly review, look back over the week: your tickets, the threads you follow, and anything "
    "left open, and act on what needs it."
)
_SUMMARY = re.compile(r"(?P<author>\S+) (?:mentioned you|posted) in thread (?P<thread>\d+)\b")
_DIGEST_THREADS = 5
_DIGEST_AUTHORS = 3
_DIGEST_BYTES = 400
_ELLIPSIS = "…"
_RETRY_SECONDS = 10
DELIVERY_BUDGET_SECONDS = TIMEOUT_SECONDS + 10
logger = logging.getLogger(__name__)

type Wake = dict[str, Any]
type InboxPrincipal = Literal["me", "human"]
type Deliver = Callable[[list[Wake]], Sequence[int | None]]


def prompt_for(wakes: Sequence[Wake]) -> str:
    summaries = "; ".join(wake["summary"] for wake in wakes)
    sentences = [f"You were woken by the agent board: {summaries}.", _ALREADY_ACKED, _ANSWER_THREADS]
    if any(wake["reason"] == TIMER_REASON for wake in wakes):
        sentences.append(_CHECK_TICKETS)
    if any(wake["reason"] == WEEKLY_REASON for wake in wakes):
        sentences.append(_WEEKLY_REVIEW)
    return " ".join(sentences)


def model_for(wakes: Sequence[Wake], models: Mapping[str, str]) -> str | None:
    reasons = {wake["reason"] for wake in wakes}
    for reason in _MODEL_PRIORITY:
        if reason in reasons and reason in models:
            return models[reason]
    return models.get("default")


def run_once(cfg: RunnerConfig, clients: Mapping[Identity, Client], happier: Happier) -> None:
    for inbox in _inboxes(cfg, clients, happier):
        inbox.drain()


def run_forever(cfg: RunnerConfig, clients: Mapping[Identity, Client], happier: Happier) -> NoReturn:
    failures: queue.SimpleQueue[BaseException] = queue.SimpleQueue()

    def serve(inbox: _Inbox) -> None:
        try:
            inbox.serve(cfg.poll_wait)
        except BaseException as failure:
            logger.exception("%s stopped", inbox.label)
            failures.put(failure)

    for inbox in _inboxes(cfg, clients, happier):
        threading.Thread(target=serve, args=(inbox,), name=inbox.label, daemon=True).start()
    raise failures.get()


def open_session(
    settings: KindConfig, me: Identity, happier: Happier, prompt: str, model: str | None = None
) -> Session:
    session = happier.create(
        backend=settings.backend,
        path=settings.workdir,
        tag=f"board-{me.kind}",
        title=f"agent board: {me.principal}",
        prompt=prompt,
        permission_mode=settings.permission_mode,
        model=model,
    )
    (ensure_state_dir(me.kind) / SESSION_FILE).write_text(json.dumps({"session_id": session.id}))
    logger.info("%s: opened session %s", me.principal, session.id)
    return session


class _Inbox:
    def __init__(
        self,
        label: str,
        client: Client,
        principal: InboxPrincipal,
        deliver: Deliver,
        coalesce_seconds: float = 0,
        scheduled: Callable[[], list[Wake]] | None = None,
    ) -> None:
        self.label = label
        self._client = client
        self._principal = principal
        self._deliver = deliver
        self._coalesce_seconds = coalesce_seconds
        self._scheduled = scheduled

    def drain(self) -> None:
        wakes: list[Wake] = []
        while leased := self._poll(0):
            wakes += leased
        self._deliver_then_ack(wakes)

    def serve(self, poll_wait: float) -> NoReturn:
        while True:
            try:
                self._deliver_then_ack(self._gather(poll_wait))
            except (BoardError, BoardUnreachable) as error:
                logger.warning("%s: %s; retrying in %ds", self.label, error, _RETRY_SECONDS)
                time.sleep(_RETRY_SECONDS)
            except Exception:
                logger.exception("%s: unexpected failure; retrying in %ds", self.label, _RETRY_SECONDS)
                time.sleep(_RETRY_SECONDS)

    def _gather(self, poll_wait: float) -> list[Wake]:
        wakes = self._poll(poll_wait)
        window_closes = min(time.monotonic() + self._coalesce_seconds, _delivery_deadline(wakes))
        while wakes and (remaining := window_closes - time.monotonic()) > 0:
            wakes += self._poll(min(remaining, poll_wait))
        return wakes

    def _poll(self, wait: float) -> list[Wake]:
        leased: list[Wake] = self._client.get("/api/wakes", wait=wait, principal=self._principal)
        return leased

    def _deliver_then_ack(self, wakes: list[Wake]) -> None:
        if self._scheduled is not None:
            wakes = wakes + self._scheduled()
        if not wakes:
            return
        delivered = [wake_id for wake_id in self._deliver(wakes) if wake_id is not None]
        if delivered:
            self._client.post("/api/wakes/ack", {"ids": delivered, "principal": self._principal})


class _AgentWaker:
    def __init__(self, settings: KindConfig, me: Identity, happier: Happier) -> None:
        self._settings = settings
        self._me = me
        self._happier = happier
        self._weekly = (
            None
            if settings.weekly is None
            else WeeklyClock(settings.weekly, ensure_state_dir(me.kind) / WEEKLY_FILE)
        )

    def scheduled(self) -> list[Wake]:
        if self._weekly is None or not self._weekly.due(time.time()):
            return []
        return [{"id": None, "reason": WEEKLY_REASON, "summary": _WEEKLY_SUMMARY}]

    def __call__(self, wakes: list[Wake]) -> list[int]:
        try:
            delivered = self._deliver(prompt_for(wakes), model_for(wakes, self._settings.models))
        except (HappierError, RemoteControlError) as error:
            _warn_human(
                self._happier,
                f"could not wake {self._me.principal} ({error}); {len(wakes)} wake(s) left leased for redelivery",
            )
            return []
        if not delivered:
            return []
        if self._weekly is not None and any(wake["reason"] == WEEKLY_REASON for wake in wakes):
            self._weekly.mark(time.time())
        return [wake["id"] for wake in wakes]

    def _deliver(self, prompt: str, model: str | None) -> bool:
        recorded = _recorded_session(self._me.kind)
        state = None if recorded is None else self._happier.status(recorded)
        if recorded is not None and state is not None and state.active:
            if state.busy:
                logger.info(
                    "%s: session %s is busy; wakes left leased for redelivery", self._me.principal, recorded
                )
                return False
            self._happier.send(recorded, prompt, model)
            logger.info(
                "%s: sent wakes to session %s (model %s)", self._me.principal, recorded, model or "default"
            )
            return True
        try:
            open_session(self._settings, self._me, self._happier, prompt, model)
        except HappierError as error:
            if self._settings.fallback != REMOTE_CONTROL or not remote_control.serves(self._me.kind):
                raise
            logger.warning(
                "%s: create failed (%s); falling back to remote control", self._me.principal, error
            )
            remote_control.start(self._settings.workdir, f"board-{self._me.kind}", prompt)
        return True


class _HumanNotifier:
    def __init__(self, happier: Happier, board_url: str) -> None:
        self._happier = happier
        self._board_url = board_url.rstrip("/")

    def __call__(self, wakes: list[Wake]) -> list[int]:
        try:
            self._happier.notify(NOTIFY_TITLE, human_digest(wakes, self._board_url))
        except HappierError as error:
            logger.warning("could not notify the human of %d wake(s): %s", len(wakes), error)
            return []
        return [wake["id"] for wake in wakes]


def human_digest(wakes: Sequence[Wake], board_url: str) -> str:
    if len(wakes) == 1:
        (wake,) = wakes
        found = _SUMMARY.match(wake["summary"])
        link = f"{board_url}/t/{found['thread']}" if found else f"{board_url}/mentions"
        return f"{_fit(wake['summary'])}\n{link}"
    authors_by_thread: dict[str, list[str]] = {}
    count_by_thread: dict[str, int] = {}
    for wake in wakes:
        found = _SUMMARY.match(wake["summary"])
        if found is None:
            continue
        thread = found["thread"]
        count_by_thread[thread] = count_by_thread.get(thread, 0) + 1
        authors = authors_by_thread.setdefault(thread, [])
        if found["author"] not in authors:
            authors.append(found["author"])
    shown = list(authors_by_thread)[:_DIGEST_THREADS]
    places = [f"#{thread} ({_names(authors_by_thread[thread])})" for thread in shown]
    other = len(wakes) - sum(count_by_thread[thread] for thread in shown)
    if other:
        places.append(f"{other} other")
    noun = "mention" if all(wake.get("reason") == "mention" for wake in wakes) else "board update"
    return f"{_fit(f'{len(wakes)} {noun}s: {", ".join(places)}')}\n{board_url}/mentions"


def _fit(text: str) -> str:
    encoded = text.encode()
    if len(encoded) <= _DIGEST_BYTES:
        return text
    return encoded[: _DIGEST_BYTES - len(_ELLIPSIS.encode())].decode(errors="ignore") + _ELLIPSIS


def _names(authors: list[str]) -> str:
    extra = len(authors) - _DIGEST_AUTHORS
    return ", ".join(authors[:_DIGEST_AUTHORS]) + (f" +{extra}" if extra > 0 else "")


def _inboxes(cfg: RunnerConfig, clients: Mapping[Identity, Client], happier: Happier) -> list[_Inbox]:
    inboxes = []
    for me, client in clients.items():
        waker = _AgentWaker(cfg.kinds[me.kind], me, happier)
        inboxes.append(_Inbox(me.principal, client, MY_INBOX, waker, cfg.coalesce_seconds, waker.scheduled))
    if cfg.notify_human and clients:
        # The board lets any agent's token lease and ack the human inbox, so the first kind relays it.
        relay_client = next(iter(clients.values()))
        notifier = _HumanNotifier(happier, relay_client.board_url)
        inboxes.append(_Inbox(HUMAN_INBOX, relay_client, HUMAN_INBOX, notifier))
    return inboxes


def _delivery_deadline(wakes: Sequence[Wake]) -> float:
    lease_ends = [ends for wake in wakes if (ends := _lease_end(wake)) is not None]
    if not lease_ends:
        return float("inf")
    return time.monotonic() + min(lease_ends) - time.time() - DELIVERY_BUDGET_SECONDS


def _lease_end(wake: Wake) -> float | None:
    try:
        return datetime.fromisoformat(wake["leased_until"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def _recorded_session(kind: str) -> str | None:
    try:
        session_id = json.loads((state_dir(kind) / SESSION_FILE).read_text())["session_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return session_id if isinstance(session_id, str) and session_id else None


def _warn_human(happier: Happier, message: str) -> None:
    logger.warning("%s", message)
    try:
        happier.notify(NOTIFY_TITLE, message)
    except HappierError as error:
        logger.warning("could not notify the human: %s", error)
