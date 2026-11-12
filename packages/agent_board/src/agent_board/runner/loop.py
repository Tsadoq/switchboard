import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal, NoReturn

from agent_board.client import BoardError, BoardUnreachable, Client
from agent_board.identity import Identity, ensure_state_dir
from agent_board.runner.activity import ActivityReporter, threads_of
from agent_board.runner.config import KindConfig, RunnerConfig
from agent_board.runner.happier import TIMEOUT_SECONDS, Happier, HappierError
from agent_board.runner.remote_control import RemoteControlError
from agent_board.runner.sessions import KIND_KEY, NOTIFY_TITLE, SessionPool, topic_key, warn_human
from agent_board.runner.weekly import WeeklyClock

MY_INBOX: Final = "me"
HUMAN_INBOX: Final = "human"
TIMER_REASON: Final = "timer"
WEEKLY_REASON: Final = "weekly"
WEEKLY_FILE = "runner-weekly.json"
HOUSEKEEP_SECONDS = 60
_THREAD_SCOPE = "thread"
_SCHEDULED_REASONS = (TIMER_REASON, WEEKLY_REASON)
_MODEL_PRIORITY = (WEEKLY_REASON, "mention", "follow", TIMER_REASON)
_ALREADY_ACKED = (
    "The runner has already acknowledged these wakes, so `agent-board inbox` will not list them "
    "and there is nothing to ack."
)
_ANSWER_RULES = (
    'Reply with `agent-board reply <id> "..."` only when a post asks you something, needs action from you, '
    "or you have something new to add; a post that only informs needs no reply. "
    "Always reply to a post by the human that responds to you, even if only to confirm."
)
_ANSWER_THREADS = f"Read each thread named above with `agent-board read <id>`. {_ANSWER_RULES}"
_CHECK_TICKETS = "For the scheduled wake, check your tickets with `agent-board tickets list --mine`."
_WEEKLY_SUMMARY = "the weekly scheduled review"
_WEEKLY_REVIEW = (
    "For the weekly review, look back over the week: your tickets, the threads you follow, and anything "
    "left open, and act on what needs it."
)
_SUMMARY = re.compile(r"(?P<author>\S+) (?:mentioned you|posted) in thread (?P<thread>\d+)\b")
_POST_REF = re.compile(r"post:(?P<post>[1-9][0-9]{0,18})")
_DIGEST_THREADS = 5
_DIGEST_AUTHORS = 3
_DIGEST_BYTES = 400
_ELLIPSIS = "…"
_RETRY_SECONDS = 10
DELIVERY_BUDGET_SECONDS = TIMEOUT_SECONDS + 10
logger = logging.getLogger(__name__)

type Wake = dict[str, Any]
type InboxPrincipal = Literal["me", "human"]
# Yields each group's ids once delivered, or [] for a group left leased, so the inbox can stop at the
# lease deadline between any two groups.
type Deliver = Callable[[list[Wake]], Iterator[list[int | None]]]


def prompt_for(wakes: Sequence[Wake]) -> str:
    summaries = "; ".join(wake["summary"] for wake in wakes)
    first = first_new_posts(wakes)
    sentences = [f"You were woken by the agent board: {summaries}.", _ALREADY_ACKED]
    if first:
        commands = "; ".join(
            f"`agent-board read {thread} --new --start-after {post - 1}`"
            for thread, post in sorted(first.items())
        )
        sentences.append(
            f"Read only the posts that woke you and anything newer: {commands}. "
            "If `--new` refuses, use `--after` with the same post number instead. "
            "For a thread named above without such a command, use `agent-board read <id>`."
        )
        sentences.append(_ANSWER_RULES)
    else:
        sentences.append(_ANSWER_THREADS)
    if any(wake["reason"] == TIMER_REASON for wake in wakes):
        sentences.append(_CHECK_TICKETS)
    if any(wake["reason"] == WEEKLY_REASON for wake in wakes):
        sentences.append(_WEEKLY_REVIEW)
    return " ".join(sentences)


def first_new_posts(wakes: Sequence[Wake]) -> dict[int, int]:
    """The earliest triggering post per thread, from wakes that name a post; others are left out."""
    first: dict[int, int] = {}
    for wake in wakes:
        thread, ref = wake.get("thread_id"), wake.get("source_ref")
        match = _POST_REF.fullmatch(ref) if isinstance(ref, str) else None
        if type(thread) is not int or thread <= 0 or match is None:
            continue
        post = int(match["post"])
        first[thread] = min(first.get(thread, post), post)
    return first


def session_key(wake: Wake, scope: str) -> str | None:
    """The `SessionPool` key a wake is delivered under; None means a one-shot session."""
    if scope != _THREAD_SCOPE:
        return KIND_KEY
    if wake["reason"] in _SCHEDULED_REASONS:
        return None
    if (topic := wake.get("topic_id")) is not None:
        return topic_key(topic)
    thread = _thread_in(wake["summary"])
    return None if thread is None else topic_key(thread)


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

    reporters: list[ActivityReporter] = []
    for inbox in _inboxes(cfg, clients, happier, reporters):
        threading.Thread(target=serve, args=(inbox,), name=inbox.label, daemon=True).start()
    for reporter in reporters:
        threading.Thread(target=reporter.serve, name="activity", daemon=True).start()
    raise failures.get()


class _Inbox:
    def __init__(
        self,
        label: str,
        client: Client,
        principal: InboxPrincipal,
        deliver: Deliver,
        coalesce_seconds: float = 0,
        scheduled: Callable[[], list[Wake]] | None = None,
        housekeep: Callable[[], None] | None = None,
    ) -> None:
        self.label = label
        self._client = client
        self._principal = principal
        self._deliver = deliver
        self._coalesce_seconds = coalesce_seconds
        self._scheduled = scheduled
        self._housekeep = housekeep
        self._housekept_at: float | None = None

    def drain(self) -> None:
        wakes: list[Wake] = []
        while leased := self._poll(0):
            wakes += leased
        self._deliver_then_ack(wakes, deadline=float("inf"))

    def serve(self, poll_wait: float) -> NoReturn:
        while True:
            try:
                self._housekeep_when_due()
                wakes = self._gather(poll_wait)
                self._deliver_then_ack(wakes, _delivery_deadline(wakes))
            except (BoardError, BoardUnreachable) as error:
                logger.warning("%s: %s; retrying in %ds", self.label, error, _RETRY_SECONDS)
                time.sleep(_RETRY_SECONDS)
            except Exception:
                logger.exception("%s: unexpected failure; retrying in %ds", self.label, _RETRY_SECONDS)
                time.sleep(_RETRY_SECONDS)

    def _housekeep_when_due(self) -> None:
        if self._housekeep is None:
            return
        now = time.monotonic()
        if self._housekept_at is not None and now - self._housekept_at < HOUSEKEEP_SECONDS:
            return
        self._housekept_at = now
        self._housekeep()

    def _gather(self, poll_wait: float) -> list[Wake]:
        wakes = self._poll(poll_wait)
        window_closes = min(time.monotonic() + self._coalesce_seconds, _delivery_deadline(wakes))
        while wakes and (remaining := window_closes - time.monotonic()) > 0:
            wakes += self._poll(min(remaining, poll_wait))
        return wakes

    def _poll(self, wait: float) -> list[Wake]:
        leased: list[Wake] = self._client.get("/api/wakes", wait=wait, principal=self._principal)
        return leased

    def _deliver_then_ack(self, wakes: list[Wake], deadline: float) -> None:
        """Ack each delivered group at once; past `deadline` (monotonic) the remaining groups stay leased."""
        if self._scheduled is not None:
            wakes = wakes + self._scheduled()
        if not wakes:
            return
        for group in self._deliver(wakes):
            delivered = [wake_id for wake_id in group if wake_id is not None]
            if delivered:
                self._client.post("/api/wakes/ack", {"ids": delivered, "principal": self._principal})
            if time.monotonic() > deadline:
                return


class _AgentWaker:
    def __init__(
        self,
        settings: KindConfig,
        me: Identity,
        happier: Happier,
        sessions: SessionPool,
        reporter: ActivityReporter | None = None,
    ) -> None:
        self._settings = settings
        self._me = me
        self._happier = happier
        self._sessions = sessions
        self._reporter = reporter
        self._weekly = (
            None
            if settings.weekly is None
            else WeeklyClock(settings.weekly, ensure_state_dir(me.kind) / WEEKLY_FILE)
        )

    def scheduled(self) -> list[Wake]:
        if self._weekly is None or not self._weekly.due(time.time()):
            return []
        return [{"id": None, "reason": WEEKLY_REASON, "summary": _WEEKLY_SUMMARY}]

    def __call__(self, wakes: list[Wake]) -> Iterator[list[int | None]]:
        wall_clock_deadline = _wall_clock_deadline(wakes)
        groups: dict[str | None, list[Wake]] = {}
        for wake in wakes:
            groups.setdefault(session_key(wake, self._settings.session_scope), []).append(wake)
        for key, group in groups.items():
            yield [wake["id"] for wake in group] if self._deliver(key, group, wall_clock_deadline) else []

    def _deliver(self, key: str | None, group: list[Wake], wall_clock_deadline: float) -> bool:
        try:
            delivered = self._sessions.deliver(
                key,
                prompt_for(group),
                model_for(group, self._settings.models),
                wall_clock_deadline,
                on_sent=self._on_sent(group),
            )
        except (HappierError, RemoteControlError) as error:
            warn_human(
                self._happier,
                f"could not wake {self._me.principal} ({error}); {len(group)} wake(s) left leased for redelivery",
            )
            return False
        if delivered and self._weekly is not None and any(wake["reason"] == WEEKLY_REASON for wake in group):
            self._weekly.mark(time.time())
        return delivered

    def _on_sent(self, group: list[Wake]) -> Callable[[str], None] | None:
        reporter = self._reporter
        if reporter is None:
            return None
        threads = threads_of(group)
        return lambda session_id: reporter.delivered(session_id, threads)


class _HumanNotifier:
    def __init__(self, happier: Happier, board_url: str) -> None:
        self._happier = happier
        self._board_url = board_url.rstrip("/")

    def __call__(self, wakes: list[Wake]) -> Iterator[list[int | None]]:
        try:
            self._happier.notify(NOTIFY_TITLE, human_digest(wakes, self._board_url))
        except HappierError as error:
            logger.warning("could not notify the human of %d wake(s): %s", len(wakes), error)
            return
        yield [wake["id"] for wake in wakes]


def human_digest(wakes: Sequence[Wake], board_url: str) -> str:
    if len(wakes) == 1:
        (wake,) = wakes
        thread = _thread_in(wake["summary"])
        link = f"{board_url}/t/{thread}" if thread else f"{board_url}/mentions"
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


def _thread_in(summary: str) -> str | None:
    found = _SUMMARY.match(summary)
    return None if found is None else found["thread"]


def _fit(text: str) -> str:
    encoded = text.encode()
    if len(encoded) <= _DIGEST_BYTES:
        return text
    return encoded[: _DIGEST_BYTES - len(_ELLIPSIS.encode())].decode(errors="ignore") + _ELLIPSIS


def _names(authors: list[str]) -> str:
    extra = len(authors) - _DIGEST_AUTHORS
    return ", ".join(authors[:_DIGEST_AUTHORS]) + (f" +{extra}" if extra > 0 else "")


def _inboxes(
    cfg: RunnerConfig,
    clients: Mapping[Identity, Client],
    happier: Happier,
    reporters: list[ActivityReporter] | None = None,
) -> list[_Inbox]:
    """Passing `reporters` gives each agent an `ActivityReporter`, appended there for the caller to run."""
    inboxes = []
    for me, client in clients.items():
        settings = cfg.kinds[me.kind]
        sessions = SessionPool(settings, me, happier)
        reporter = None
        if reporters is not None:
            reporter = ActivityReporter(client, happier)
            reporters.append(reporter)
        waker = _AgentWaker(settings, me, happier, sessions, reporter)
        inboxes.append(
            _Inbox(
                me.principal,
                client,
                MY_INBOX,
                waker,
                cfg.coalesce_seconds,
                waker.scheduled,
                sessions.housekeep,
            )
        )
    if cfg.notify_human and clients:
        # The board lets any agent's token lease and ack the human inbox, so the first kind relays it.
        relay_client = next(iter(clients.values()))
        notifier = _HumanNotifier(happier, relay_client.board_url)
        inboxes.append(_Inbox(HUMAN_INBOX, relay_client, HUMAN_INBOX, notifier))
    return inboxes


def _delivery_deadline(wakes: Sequence[Wake]) -> float:
    """`_wall_clock_deadline` on the `time.monotonic` timebase the inbox waits on."""
    return time.monotonic() + _wall_clock_deadline(wakes) - time.time()


def _wall_clock_deadline(wakes: Sequence[Wake]) -> float:
    """The `time.time` after which delivering `wakes` may outlast their leases (`SessionPool`'s timebase)."""
    lease_ends = [ends for wake in wakes if (ends := _lease_end(wake)) is not None]
    return min(lease_ends, default=float("inf")) - DELIVERY_BUDGET_SECONDS


def _lease_end(wake: Wake) -> float | None:
    try:
        return datetime.fromisoformat(wake["leased_until"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
