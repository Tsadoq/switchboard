import json
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.identity import Identity, state_dir
from agent_board.runner import loop, remote_control
from agent_board.runner.config import KindConfig, RunnerConfig
from agent_board.runner.happier import Happier, HappierError
from agent_board.runner.loop import Deliver, Wake, _Inbox, session_key
from agent_board.runner.weekly import WeeklySlot

if TYPE_CHECKING:
    from .conftest import FakeSign

_EPOCH = 1_800_000_000.0
_LEASE_SECONDS = 120


class Stopped(BaseException):
    pass


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def time(self) -> float:
        return _EPOCH + self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


class FakeClient:
    def __init__(
        self,
        clock: FakeClock,
        batches: list[list[Wake]],
        lease_seconds: float = _LEASE_SECONDS,
        polls: int | None = None,
    ) -> None:
        self._clock = clock
        self._batches = batches
        self._lease_seconds = lease_seconds
        self._polls = polls
        self.waits: list[float] = []
        self.ack_posts: list[list[int]] = []
        self.stopped = threading.Event()

    def get(self, path: str, *, wait: float, principal: str) -> list[Wake]:
        if self._polls is not None and len(self.waits) >= self._polls:
            raise Stopped
        self.waits.append(wait)
        if self.stopped.wait(0.01 if not self._batches else 0):
            raise Stopped
        if not self._batches:
            self._clock.sleep(wait)
            return []
        leased_until = datetime.fromtimestamp(self._clock.time() + self._lease_seconds, UTC)
        return [{**wake, "leased_until": leased_until.isoformat()} for wake in self._batches.pop(0)]

    def post(self, path: str, payload: dict[str, Any]) -> None:
        self.ack_posts.append(payload["ids"])


def _wake(wake_id: int) -> Wake:
    return {"id": wake_id, "summary": f"wake {wake_id}", "reason": "mention"}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(loop, "time", fake)
    return fake


def _inbox(
    client: FakeClient,
    deliver: Deliver,
    coalesce: float,
    housekeep: Callable[[], None] | None = None,
) -> _Inbox:
    return _Inbox("pi@testbox", client, "me", deliver, coalesce, housekeep=housekeep)  # type: ignore[arg-type]


def _nothing(wakes: list[Wake]) -> Iterator[list[int | None]]:
    yield from ()


def test_wakes_within_the_window_are_gathered_together(clock: FakeClock) -> None:
    client = FakeClient(clock, [[_wake(1)], [_wake(2)]])
    inbox = _inbox(client, _nothing, coalesce=4)

    gathered = inbox._gather(poll_wait=1)

    assert [wake["id"] for wake in gathered] == [1, 2], f"not coalesced: {gathered}"
    assert clock.elapsed == pytest.approx(4), f"window not honoured: {clock.elapsed}"


class ArrivingClient:
    """A board whose wakes become leasable at set times; a poll returns early when one is due."""

    def __init__(self, clock: FakeClock, arrivals: list[tuple[float, Wake]], lease_seconds: float) -> None:
        self._clock = clock
        self._arrivals = sorted(arrivals, key=lambda arrival: arrival[0])
        self._lease_seconds = lease_seconds
        self.ack_posts: list[list[int]] = []

    def get(self, path: str, *, wait: float, principal: str) -> list[Wake]:
        if not self._due():
            pending = [at for at, _ in self._arrivals]
            wakes_at = min([*pending, self._clock.elapsed + wait])
            self._clock.sleep(max(0.0, min(wakes_at, self._clock.elapsed + wait) - self._clock.elapsed))
        leased_until = datetime.fromtimestamp(self._clock.time() + self._lease_seconds, UTC).isoformat()
        due, self._arrivals = self._due(), [a for a in self._arrivals if a[0] > self._clock.elapsed]
        return [{**wake, "leased_until": leased_until} for wake in due]

    def post(self, path: str, payload: dict[str, Any]) -> None:
        self.ack_posts.append(payload["ids"])

    def _due(self) -> list[Wake]:
        return [wake for at, wake in self._arrivals if at <= self._clock.elapsed]


def test_ten_posts_in_sixty_seconds_are_delivered_as_one_prompt_and_acked_together(clock: FakeClock) -> None:
    burst = [
        (
            6.0 * n,
            {
                "id": n + 1,
                "reason": "follow",
                "summary": 'pi@testbox posted in thread 9 "Backups"',
                "thread_id": 9,
                "source_ref": f"post:{100 + n}",
            },
        )
        for n in range(10)
    ]
    client = ArrivingClient(clock, burst, lease_seconds=300)
    prompts: list[str] = []

    def deliver(wakes: list[Wake]) -> Iterator[list[int | None]]:
        prompts.append(loop.prompt_for(wakes))
        yield [wake["id"] for wake in wakes]

    inbox = _Inbox("pi@testbox", client, "me", deliver, 120)  # type: ignore[arg-type]

    wakes = inbox._gather(poll_wait=30)
    inbox._deliver_then_ack(wakes, deadline=float("inf"))

    assert len(prompts) == 1, f"a 60 s burst was split into {len(prompts)} prompts"
    assert 'pi@testbox posted in thread 9 "Backups" (10 posts)' in prompts[0], prompts[0]
    assert "`agent-board read 9 --after 99`" in prompts[0], prompts[0]
    assert client.ack_posts == [list(range(1, 11))], f"burst not acked as one: {client.ack_posts}"
    assert clock.elapsed == pytest.approx(120), f"window not honoured: {clock.elapsed}"


def test_window_closes_before_a_short_lease_leaves_no_time_to_deliver(clock: FakeClock) -> None:
    lease_seconds = 30
    client = FakeClient(clock, [[_wake(1)]], lease_seconds=lease_seconds)
    inbox = _inbox(client, _nothing, coalesce=60)

    inbox._gather(poll_wait=60)

    assert clock.elapsed == 0, (
        f"window kept open past the lease budget: {clock.elapsed}s, waits {client.waits}"
    )


def test_window_is_trimmed_to_the_lease_minus_the_delivery_budget(clock: FakeClock) -> None:
    client = FakeClient(clock, [[_wake(1)]], lease_seconds=_LEASE_SECONDS)
    inbox = _inbox(client, _nothing, coalesce=60)

    inbox._gather(poll_wait=60)

    budget = _LEASE_SECONDS - loop.DELIVERY_BUDGET_SECONDS
    assert clock.elapsed == pytest.approx(min(60, budget)), f"window not trimmed: {clock.elapsed}"


def test_unexpected_delivery_error_does_not_stop_the_inbox(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(clock, [[_wake(1)], [_wake(1)]])
    attempts: list[list[int]] = []
    delivered_twice = threading.Event()

    def deliver(wakes: list[Wake]) -> Iterator[list[int | None]]:
        attempts.append([wake["id"] for wake in wakes])
        if len(attempts) == 1:
            raise OSError("disk full writing runner-sessions.json")
        delivered_twice.set()
        yield [wake["id"] for wake in wakes]

    inbox = _inbox(client, deliver, coalesce=0)

    def serve() -> None:
        try:
            inbox.serve(0)
        except Stopped:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    delivered = delivered_twice.wait(5)
    client.stopped.set()
    thread.join(5)

    assert delivered, f"inbox stopped after the first failure: {attempts}"
    assert client.ack_posts[:1] == [[1]], f"wake not acked on retry: {client.ack_posts}"


class FakeHappier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        if self.fail:
            raise HappierError("exit_1")
        self.sent.append((title, message))


def _human_wake(wake_id: int, author: str, thread: int) -> Wake:
    return {
        "id": wake_id,
        "reason": "mention",
        "summary": f'{author} mentioned you in thread {thread} "T{thread}"',
    }


def test_one_human_wake_links_to_its_thread() -> None:
    message = loop.human_digest([_human_wake(1, "pi@dev", 29)], "https://board.example")

    assert message == 'pi@dev mentioned you in thread 29 "T29"\nhttps://board.example/t/29', message


def test_several_human_wakes_become_one_push_grouped_by_thread() -> None:
    happier = FakeHappier()
    notifier = loop._HumanNotifier(happier, "https://board.example/")  # type: ignore[arg-type]
    wakes = [
        _human_wake(1, "pi@dev", 29),
        _human_wake(2, "claude@ops", 29),
        _human_wake(3, "pi@heimdall", 19),
        _human_wake(4, "pi@dev", 29),
    ]

    acked = list(notifier(wakes))

    assert acked == [[1, 2, 3, 4]], acked
    assert happier.sent == [
        (
            "agent board",
            "4 mentions: #29 (pi@dev, claude@ops), #19 (pi@heimdall)\nhttps://board.example/mentions",
        )
    ], happier.sent


def test_unrecognised_summaries_still_count_in_the_digest() -> None:
    wakes = [_human_wake(1, "pi@dev", 29), {"id": 2, "reason": "ticket", "summary": "something else"}]

    message = loop.human_digest(wakes, "https://board.example")

    assert message == "2 board updates: #29 (pi@dev), 1 other\nhttps://board.example/mentions", message


def test_failed_digest_push_acks_nothing() -> None:
    notifier = loop._HumanNotifier(FakeHappier(fail=True), "https://board.example")  # type: ignore[arg-type]

    assert list(notifier([_human_wake(1, "pi@dev", 29), _human_wake(2, "pi@dev", 30)])) == []


def test_a_large_digest_stays_short_and_keeps_its_link() -> None:
    wakes = [_human_wake(i, f"agent{i % 5}@box", 100 + i // 2) for i in range(60)]

    message = loop.human_digest(wakes, "https://board.example")

    summary, link = message.split("\n")
    assert link == "https://board.example/mentions", message
    assert summary.startswith("60 mentions: #100 (agent0@box, agent1@box), #101 "), summary
    assert summary.endswith(", 50 other"), summary
    assert len(message) < 300, len(message)


def _budgeted(message: str, link: str) -> str:
    summary, _, tail = message.rpartition("\n")
    assert tail == link, message
    assert len(summary.encode()) <= loop._DIGEST_BYTES, len(summary.encode())
    return summary


def test_a_huge_title_is_cut_to_the_byte_budget_and_keeps_the_thread_link() -> None:
    wake = {"id": 1, "reason": "mention", "summary": f'pi@dev mentioned you in thread 29 "{"x" * 10_000}"'}

    summary = _budgeted(loop.human_digest([wake], "https://board.example"), "https://board.example/t/29")

    assert summary.startswith('pi@dev mentioned you in thread 29 "xxx') and summary.endswith("…"), summary


def test_long_author_labels_are_cut_to_the_byte_budget() -> None:
    wakes = [_human_wake(i, f"{'a' * 300}{i}@box", 10 + i // 3) for i in range(15)]

    summary = _budgeted(loop.human_digest(wakes, "https://board.example"), "https://board.example/mentions")

    assert summary.startswith("15 mentions: #10 (") and summary.endswith("…"), summary


def test_multibyte_titles_are_cut_on_a_character_boundary() -> None:
    wake = {"id": 1, "reason": "mention", "summary": f'pi@dev mentioned you in thread 7 "{"é🙂" * 500}"'}

    summary = _budgeted(loop.human_digest([wake], "https://board.example"), "https://board.example/t/7")

    assert "\ufffd" not in summary, summary
    assert summary.endswith(("é…", "🙂…")), summary


def test_a_cut_digest_is_still_sent_once_and_acked() -> None:
    happier = FakeHappier()
    notifier = loop._HumanNotifier(happier, "https://board.example")  # type: ignore[arg-type]
    wakes = [_human_wake(1, "a" * 5000 + "@box", 3), _human_wake(2, "pi@dev", 4)]

    assert list(notifier(wakes)) == [[1, 2]]
    assert len(happier.sent) == 1 and happier.sent[0][1].endswith("\nhttps://board.example/mentions"), (
        happier.sent
    )


def _serve_until_stopped(inbox: _Inbox, poll_wait: float) -> None:
    with pytest.raises(Stopped):
        inbox.serve(poll_wait)


@pytest.mark.parametrize(
    ("scope", "wake", "key"),
    [
        ("kind", {"reason": "timer", "summary": "scheduled wake"}, "kind"),
        ("kind", {"reason": "mention", "summary": "a mentioned you in thread 9", "topic_id": 7}, "kind"),
        ("thread", {"reason": "mention", "summary": "a mentioned you in thread 9", "topic_id": 7}, "topic:7"),
        ("thread", {"reason": "follow", "summary": 'a posted in thread 9 "T"', "topic_id": None}, "topic:9"),
        ("thread", {"reason": "mention", "summary": "a mentioned you in thread 9"}, "topic:9"),
        ("thread", {"reason": "ticket", "summary": "ticket 4 was assigned to you"}, None),
        ("thread", {"reason": "timer", "summary": "scheduled wake", "topic_id": 7}, None),
        ("thread", {"reason": "weekly", "summary": "the weekly scheduled review"}, None),
    ],
)
def test_session_key_prefers_topic_then_summary(scope: str, wake: Wake, key: str | None) -> None:
    assert session_key(wake, scope) == key


def _slow_groups(clock: FakeClock, client: FakeClient, pulled: list[int]) -> Deliver:
    def deliver(wakes: list[Wake]) -> Iterator[list[int | None]]:
        for wake in wakes:
            pulled.append(wake["id"])
            clock.sleep(60)
            client.stopped.set()
            yield [wake["id"]]

    return deliver


def test_groups_after_the_lease_deadline_stay_leased(clock: FakeClock) -> None:
    client = FakeClient(clock, [[_wake(1), _wake(2)]])
    pulled: list[int] = []
    inbox = _inbox(client, _slow_groups(clock, client, pulled), coalesce=0)

    _serve_until_stopped(inbox, 0)

    assert client.ack_posts == [[1]], f"a group past the lease deadline was acked: {client.ack_posts}"
    assert pulled == [1], f"a group was delivered past the lease deadline: {pulled}"


def test_drain_delivers_every_group(clock: FakeClock) -> None:
    client = FakeClient(clock, [[_wake(1), _wake(2)]])
    inbox = _inbox(client, _slow_groups(clock, client, []), coalesce=0)

    inbox.drain()

    assert client.ack_posts == [[1], [2]], f"drain did not ack each group: {client.ack_posts}"


def test_housekeeping_runs_before_a_poll_at_most_once_a_minute(clock: FakeClock) -> None:
    client = FakeClient(clock, [], polls=7)
    housekept: list[tuple[float, int]] = []
    inbox = _inbox(
        client, _nothing, coalesce=0, housekeep=lambda: housekept.append((clock.elapsed, len(client.waits)))
    )

    _serve_until_stopped(inbox, 25)

    assert housekept == [(0, 0), (75, 3), (150, 6)], f"housekeeping mistimed: {housekept}"


_MYTHOS_POLICY = {"default": "sonnet", "mention": "mythos", "weekly": "mythos"}
_ME = Identity("claude@testbox", "claude", "testbox")


@pytest.fixture
def happier_state(fake_sign: "FakeSign", tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "happier-state.json"
    state.write_text("")
    monkeypatch.setenv("FAKE_HAPPIER_STATE", str(state))
    monkeypatch.delenv("FAKE_HAPPIER_FAIL", raising=False)
    return state


def _sessions(happier_state: Path) -> list[dict[str, Any]]:
    text = happier_state.read_text()
    sessions: list[dict[str, Any]] = json.loads(text)["sessions"] if text.strip() else []
    return sessions


def _agent_inbox(
    client: FakeClient, scope: str = "thread", fallback: str = "none", **settings: Any
) -> _Inbox:
    kind = KindConfig(
        "claude", Path.home() / "work", "default", "agent:claude", fallback, session_scope=scope, **settings
    )
    cfg = RunnerConfig({"claude": kind}, coalesce_seconds=0, poll_wait=1, notify_human=False)
    (inbox,) = loop._inboxes(cfg, {_ME: client}, Happier())  # type: ignore[dict-item]
    return inbox


def _topic_wake(wake_id: int, topic: int, reason: str = "mention") -> Wake:
    return {"id": wake_id, "reason": reason, "summary": f"wake {wake_id} in topic {topic}", "topic_id": topic}


def _drain_wakes(wakes: list[Wake], scope: str = "thread", **settings: Any) -> FakeClient:
    client = FakeClient(FakeClock(), [wakes])
    _agent_inbox(client, scope, **settings).drain()
    return client


def test_batch_is_split_into_one_delivery_per_topic(happier_state: Path) -> None:
    client = _drain_wakes([_topic_wake(1, 7), _topic_wake(2, 9), _topic_wake(3, 7)])

    assert client.ack_posts == [[1, 3], [2]], client.ack_posts
    prompts = [session["messages"] for session in _sessions(happier_state)]
    assert len(prompts) == 2, prompts
    assert "wake 1 in topic 7" in prompts[0][0] and "wake 3 in topic 7" in prompts[0][0], prompts
    assert "topic 9" not in prompts[0][0] and "topic 7" not in prompts[1][0], prompts


def test_a_failed_topic_leaves_only_its_wakes_leased(
    happier_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drain_wakes([_topic_wake(1, 7)])
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "create:boom")

    client = _drain_wakes([_topic_wake(2, 7), _topic_wake(3, 9), _topic_wake(4, 7)])

    assert client.ack_posts == [[2, 4]], f"only topic 7 was delivered: {client.ack_posts}"
    notifications = json.loads(happier_state.read_text())["notifications"]
    assert len(notifications) == 1 and "boom" in notifications[0]["message"], notifications


def test_timer_and_weekly_wakes_never_reach_a_topic_session(happier_state: Path) -> None:
    timer = {"id": 2, "reason": "timer", "summary": "scheduled wake, every 21600 s"}

    client = _drain_wakes([_topic_wake(1, 7), timer])

    assert client.ack_posts == [[1], [2]], client.ack_posts
    prompts = [session["messages"] for session in _sessions(happier_state)]
    assert len(prompts) == 2, prompts
    assert "every 21600 s" not in prompts[0][0] and "agent-board read 7" in prompts[0][0], prompts
    assert "every 21600 s" in prompts[1][0] and "topic 7" not in prompts[1][0], prompts


def _due_weekly() -> Path:
    weekly_file = state_dir(_ME.kind) / loop.WEEKLY_FILE
    weekly_file.parent.mkdir(parents=True, exist_ok=True)
    weekly_file.write_text('{"last_fired_at": 0}')
    return weekly_file


def test_weekly_clock_is_marked_only_when_its_group_was_delivered(
    happier_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    weekly = WeeklySlot.parse("Mon 06:00 UTC", "")
    _drain_wakes([_topic_wake(1, 7)])
    weekly_file = _due_weekly()
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "create:boom")

    failed = _drain_wakes([_topic_wake(2, 7)], weekly=weekly)

    assert failed.ack_posts == [[2]], failed.ack_posts
    assert weekly_file.read_text() == '{"last_fired_at": 0}', "weekly marked though its group failed"
    monkeypatch.delenv("FAKE_HAPPIER_FAIL")
    _drain_wakes([], weekly=weekly)
    assert json.loads(weekly_file.read_text())["last_fired_at"] > 0, "delivered weekly not marked"


def test_each_topic_uses_its_own_reason_model(happier_state: Path) -> None:
    _drain_wakes([_topic_wake(1, 7), _topic_wake(2, 9, reason="follow")], models=_MYTHOS_POLICY)

    assert [session["models"] for session in _sessions(happier_state)] == [["mythos"], ["sonnet"]]


def test_create_log_line_redacts_the_prompt(happier_state: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="agent_board")

    _drain_wakes([_topic_wake(1, 7)])

    creates = [record.getMessage() for record in caplog.records if "session create" in record.getMessage()]
    assert len(creates) == 1 and "<redacted>" in creates[0], creates
    assert "wake 1 in topic 7" not in caplog.text, caplog.text


def test_one_session_switches_model_on_every_delivery(happier_state: Path) -> None:
    for wake_id, reason in enumerate(("mention", "timer", "mention", "follow")):
        _drain_wakes([{"id": wake_id, "reason": reason, "summary": reason}], "kind", models=_MYTHOS_POLICY)

    assert [session["models"] for session in _sessions(happier_state)] == [
        ["mythos", "sonnet", "mythos", "sonnet"]
    ]


def test_no_remote_control_fallback_when_a_model_is_required(
    happier_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(remote_control, "start", lambda *args: started.append("started"))
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "create:boom")
    weekly = WeeklySlot.parse("Mon 06:00 UTC", "")
    weekly_file = _due_weekly()

    client = _drain_wakes([_wake(1)], "kind", models=_MYTHOS_POLICY, weekly=weekly, fallback="remote-control")

    assert client.ack_posts == [], client.ack_posts
    assert started == [], "fell back to remote control without the chosen model"
    assert weekly_file.read_text() == '{"last_fired_at": 0}', "weekly marked though undelivered"


def test_remote_control_fallback_still_works_without_models(
    happier_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(remote_control, "start", lambda *args: started.append("started"))
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "create:boom")

    client = _drain_wakes([_wake(1)], "kind", fallback="remote-control")

    assert client.ack_posts == [[1]], client.ack_posts
    assert started == ["started"]
