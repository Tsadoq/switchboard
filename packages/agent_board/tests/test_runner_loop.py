import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agent_board.identity import Identity
from agent_board.runner import loop, remote_control
from agent_board.runner.config import KindConfig
from agent_board.runner.happier import HappierError, Session, SessionState
from agent_board.runner.loop import Wake, _Inbox
from agent_board.runner.weekly import WeeklySlot

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
        self, clock: FakeClock, batches: list[list[Wake]], lease_seconds: float = _LEASE_SECONDS
    ) -> None:
        self._clock = clock
        self._batches = batches
        self._lease_seconds = lease_seconds
        self.waits: list[float] = []
        self.acked: list[int] = []
        self.stopped = threading.Event()

    def get(self, path: str, *, wait: float, principal: str) -> list[Wake]:
        self.waits.append(wait)
        if self.stopped.wait(0.01 if not self._batches else 0):
            raise Stopped
        if not self._batches:
            self._clock.sleep(wait)
            return []
        leased_until = datetime.fromtimestamp(self._clock.time() + self._lease_seconds, UTC)
        return [{**wake, "leased_until": leased_until.isoformat()} for wake in self._batches.pop(0)]

    def post(self, path: str, payload: dict[str, Any]) -> None:
        self.acked += payload["ids"]


def _wake(wake_id: int) -> Wake:
    return {"id": wake_id, "summary": f"wake {wake_id}", "reason": "mention"}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(loop, "time", fake)
    return fake


def _inbox(client: FakeClient, deliver: Callable[[list[Wake]], list[int]], coalesce: float) -> _Inbox:
    return _Inbox("pi@testbox", client, "me", deliver, coalesce)  # type: ignore[arg-type]


def test_wakes_within_the_window_are_gathered_together(clock: FakeClock) -> None:
    client = FakeClient(clock, [[_wake(1)], [_wake(2)]])
    inbox = _inbox(client, lambda wakes: [], coalesce=4)

    gathered = inbox._gather(poll_wait=1)

    assert [wake["id"] for wake in gathered] == [1, 2], f"not coalesced: {gathered}"
    assert clock.elapsed == pytest.approx(4), f"window not honoured: {clock.elapsed}"


def test_window_closes_before_a_short_lease_leaves_no_time_to_deliver(clock: FakeClock) -> None:
    lease_seconds = 30
    client = FakeClient(clock, [[_wake(1)]], lease_seconds=lease_seconds)
    inbox = _inbox(client, lambda wakes: [], coalesce=60)

    inbox._gather(poll_wait=60)

    assert clock.elapsed == 0, (
        f"window kept open past the lease budget: {clock.elapsed}s, waits {client.waits}"
    )


def test_window_is_trimmed_to_the_lease_minus_the_delivery_budget(clock: FakeClock) -> None:
    client = FakeClient(clock, [[_wake(1)]], lease_seconds=_LEASE_SECONDS)
    inbox = _inbox(client, lambda wakes: [], coalesce=60)

    inbox._gather(poll_wait=60)

    budget = _LEASE_SECONDS - loop.DELIVERY_BUDGET_SECONDS
    assert clock.elapsed == pytest.approx(min(60, budget)), f"window not trimmed: {clock.elapsed}"


def test_unexpected_delivery_error_does_not_stop_the_inbox(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(clock, [[_wake(1)], [_wake(1)]])
    attempts: list[list[int]] = []
    delivered_twice = threading.Event()

    def deliver(wakes: list[Wake]) -> list[int]:
        attempts.append([wake["id"] for wake in wakes])
        if len(attempts) == 1:
            raise OSError("disk full writing runner-session.json")
        delivered_twice.set()
        return [wake["id"] for wake in wakes]

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
    assert client.acked[:1] == [1], f"wake not acked on retry: {client.acked}"


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

    acked = notifier(wakes)

    assert acked == [1, 2, 3, 4], acked
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

    assert notifier([_human_wake(1, "pi@dev", 29), _human_wake(2, "pi@dev", 30)]) == []


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

    assert notifier(wakes) == [1, 2]
    assert len(happier.sent) == 1 and happier.sent[0][1].endswith("\nhttps://board.example/mentions"), (
        happier.sent
    )


class SessionHappier:
    def __init__(self, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.models: list[str | None] = []

    def status(self, session_id: str) -> SessionState:
        return SessionState(active=True, pending_requests=0, controlled_by_user=False)

    def send(self, session_id: str, message: str, model: str | None = None) -> None:
        self.models.append(model)

    def create(self, **fields: Any) -> Session:
        if self.fail_create:
            raise HappierError("exit_1")
        self.models.append(fields["model"])
        return Session("cm-new", True)

    def notify(self, title: str, message: str) -> None:
        pass


def _waker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    happier: SessionHappier,
    *,
    session: str | None,
    **settings: Any,
) -> "loop._AgentWaker":
    monkeypatch.setattr(loop, "ensure_state_dir", lambda kind: tmp_path)
    monkeypatch.setattr(loop, "_recorded_session", lambda kind: session)
    kind = KindConfig("claude", tmp_path, "auto", "agent:claude", "remote-control", **settings)
    return loop._AgentWaker(kind, Identity("claude@testbox", "claude", "testbox"), happier)  # type: ignore[arg-type]


MYTHOS_POLICY = {"default": "sonnet", "mention": "mythos", "weekly": "mythos"}


def test_one_session_switches_model_on_every_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    happier = SessionHappier()
    waker = _waker(tmp_path, monkeypatch, happier, session="cm-old", models=MYTHOS_POLICY)

    for reason in ("mention", "timer", "mention", "follow"):
        assert waker([{"id": 1, "reason": reason, "summary": reason}]) == [1]

    assert happier.models == ["mythos", "sonnet", "mythos", "sonnet"]


def test_no_remote_control_fallback_when_a_model_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(remote_control, "start", lambda *args: started.append("started"))
    happier = SessionHappier(fail_create=True)
    weekly = WeeklySlot.parse("Mon 06:00 UTC", "")
    waker = _waker(tmp_path, monkeypatch, happier, session=None, models=MYTHOS_POLICY, weekly=weekly)
    (tmp_path / loop.WEEKLY_FILE).write_text('{"last_fired_at": 0}')

    acked = waker([{"id": 1, "reason": "mention", "summary": "m"}, *waker.scheduled()])

    assert acked == [], acked
    assert started == [], "fell back to remote control without the chosen model"
    assert (tmp_path / loop.WEEKLY_FILE).read_text() == '{"last_fired_at": 0}', (
        "weekly marked though undelivered"
    )


def test_remote_control_fallback_still_works_without_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(remote_control, "start", lambda *args: started.append("started"))
    waker = _waker(tmp_path, monkeypatch, SessionHappier(fail_create=True), session=None)

    assert waker([{"id": 1, "reason": "mention", "summary": "m"}]) == [1]
    assert started == ["started"]
