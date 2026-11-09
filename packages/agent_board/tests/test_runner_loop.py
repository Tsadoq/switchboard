import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from agent_board.runner import loop
from agent_board.runner.happier import HappierError
from agent_board.runner.loop import Wake, _Inbox

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
