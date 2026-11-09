import json
import logging
import queue
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

SESSION_FILE = "runner-session.json"
NOTIFY_TITLE = "agent board"
MY_INBOX: Final = "me"
HUMAN_INBOX: Final = "human"
TIMER_REASON: Final = "timer"
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
_RETRY_SECONDS = 10
DELIVERY_BUDGET_SECONDS = TIMEOUT_SECONDS + 10
logger = logging.getLogger(__name__)

type Wake = dict[str, Any]
type InboxPrincipal = Literal["me", "human"]
type Deliver = Callable[[list[Wake]], list[int]]


def prompt_for(wakes: Sequence[Wake]) -> str:
    summaries = "; ".join(wake["summary"] for wake in wakes)
    sentences = [f"You were woken by the agent board: {summaries}.", _ALREADY_ACKED, _ANSWER_THREADS]
    if any(wake["reason"] == TIMER_REASON for wake in wakes):
        sentences.append(_CHECK_TICKETS)
    return " ".join(sentences)


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


def open_session(settings: KindConfig, me: Identity, happier: Happier, prompt: str) -> Session:
    session = happier.create(
        backend=settings.backend,
        path=settings.workdir,
        tag=f"board-{me.kind}",
        title=f"agent board: {me.principal}",
        prompt=prompt,
        permission_mode=settings.permission_mode,
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
    ) -> None:
        self.label = label
        self._client = client
        self._principal = principal
        self._deliver = deliver
        self._coalesce_seconds = coalesce_seconds

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
        if not wakes:
            return
        delivered = self._deliver(wakes)
        if delivered:
            self._client.post("/api/wakes/ack", {"ids": delivered, "principal": self._principal})


class _AgentWaker:
    def __init__(self, settings: KindConfig, me: Identity, happier: Happier) -> None:
        self._settings = settings
        self._me = me
        self._happier = happier

    def __call__(self, wakes: list[Wake]) -> list[int]:
        try:
            delivered = self._deliver(prompt_for(wakes))
        except (HappierError, RemoteControlError) as error:
            _warn_human(
                self._happier,
                f"could not wake {self._me.principal} ({error}); {len(wakes)} wake(s) left leased for redelivery",
            )
            return []
        return [wake["id"] for wake in wakes] if delivered else []

    def _deliver(self, prompt: str) -> bool:
        recorded = _recorded_session(self._me.kind)
        state = None if recorded is None else self._happier.status(recorded)
        if recorded is not None and state is not None and state.active:
            if state.busy:
                logger.info(
                    "%s: session %s is busy; wakes left leased for redelivery", self._me.principal, recorded
                )
                return False
            self._happier.send(recorded, prompt)
            logger.info("%s: sent wakes to session %s", self._me.principal, recorded)
            return True
        try:
            open_session(self._settings, self._me, self._happier, prompt)
        except HappierError as error:
            if self._settings.fallback != REMOTE_CONTROL or not remote_control.serves(self._me.kind):
                raise
            logger.warning(
                "%s: create failed (%s); falling back to remote control", self._me.principal, error
            )
            remote_control.start(self._settings.workdir, f"board-{self._me.kind}", prompt)
        return True


class _HumanNotifier:
    def __init__(self, happier: Happier) -> None:
        self._happier = happier

    def __call__(self, wakes: list[Wake]) -> list[int]:
        notified: list[int] = []
        for wake in wakes:
            try:
                self._happier.notify(NOTIFY_TITLE, wake["summary"])
            except HappierError as error:
                logger.warning("could not notify the human of wake %s: %s", wake["id"], error)
                continue
            notified.append(wake["id"])
        return notified


def _inboxes(cfg: RunnerConfig, clients: Mapping[Identity, Client], happier: Happier) -> list[_Inbox]:
    inboxes = [
        _Inbox(
            me.principal, client, MY_INBOX, _AgentWaker(cfg.kinds[me.kind], me, happier), cfg.coalesce_seconds
        )
        for me, client in clients.items()
    ]
    if cfg.notify_human and clients:
        # The board lets any agent's token lease and ack the human inbox, so the first kind relays it.
        relay_client = next(iter(clients.values()))
        inboxes.append(_Inbox(HUMAN_INBOX, relay_client, HUMAN_INBOX, _HumanNotifier(happier)))
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
