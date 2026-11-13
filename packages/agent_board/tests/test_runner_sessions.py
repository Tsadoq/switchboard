import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.cursors import CURSOR_DIR, locked
from agent_board.identity import Identity, ensure_state_dir
from agent_board.runner import remote_control
from agent_board.runner.config import KindConfig
from agent_board.runner.happier import Happier
from agent_board.runner.sessions import SessionPool

if TYPE_CHECKING:
    from .conftest import FakeSign

_EPOCH = 1_800_000_000.0
_KIND = "claude"
_ME = Identity(f"{_KIND}@testbox", _KIND, "testbox")


class FakeClock:
    def __init__(self) -> None:
        self.now = _EPOCH

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class Fakes:
    happier_state: Path
    tmux_log: Path


@pytest.fixture
def box(fake_sign: "FakeSign", tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    fakes = Fakes(tmp_path / "happier-state.json", tmp_path / "tmux.log")
    fakes.happier_state.write_text("")
    monkeypatch.setenv("FAKE_HAPPIER_STATE", str(fakes.happier_state))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(fakes.tmux_log))
    monkeypatch.delenv("FAKE_HAPPIER_FAIL", raising=False)
    return fakes


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def pool(
    clock: FakeClock,
    scope: str = "thread",
    max_sessions: int = 2,
    ttl: float = 1800,
    fallback: str = "none",
    happier: Happier | None = None,
) -> SessionPool:
    settings = KindConfig(
        name=_KIND,
        workdir=Path.home() / "work",
        permission_mode="default",
        backend=f"agent:{_KIND}",
        fallback=fallback,
        session_scope=scope,
        max_sessions=max_sessions,
        idle_ttl_seconds=ttl,
    )
    return SessionPool(settings, _ME, happier or Happier(), clock)


def _happier(fakes: Fakes) -> dict[str, Any]:
    text = fakes.happier_state.read_text()
    state: dict[str, Any] = json.loads(text) if text.strip() else {}
    return {"sessions": [], "notifications": [], **state}


def _sessions(fakes: Fakes) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = _happier(fakes)["sessions"]
    return sessions


def _update_sessions(fakes: Fakes, change: Callable[[dict[str, Any]], None]) -> None:
    state = _happier(fakes)
    for entry in state["sessions"]:
        change(entry)
    fakes.happier_state.write_text(json.dumps(state))


def _tmux_calls(fakes: Fakes) -> list[list[str]]:
    if not fakes.tmux_log.exists():
        return []
    return [json.loads(line) for line in fakes.tmux_log.read_text().splitlines()]


def _archived(fakes: Fakes) -> list[str]:
    return [entry["session"]["id"] for entry in _sessions(fakes) if entry["session"].get("archivedAt")]


def _live(fakes: Fakes) -> list[str]:
    return [entry["session"]["id"] for entry in _sessions(fakes) if not entry["session"].get("archivedAt")]


def _messages(fakes: Fakes, session_id: str) -> list[str]:
    return next(entry["messages"] for entry in _sessions(fakes) if entry["session"]["id"] == session_id)


def _map(fakes: Fakes) -> dict[str | None, str]:
    path = ensure_state_dir(_KIND) / "runner-sessions.json"
    if not path.exists():
        return {}
    return {entry["key"]: entry["session_id"] for entry in json.loads(path.read_text())["sessions"]}


def _legacy_session(fakes: Fakes, mtime: float) -> str:
    session_id = (
        Happier()
        .create(
            backend="agent:claude",
            path=Path.home(),
            tag="t",
            title="T",
            prompt="hello",
            permission_mode="default",
        )
        .id
    )
    legacy = ensure_state_dir(_KIND) / "runner-session.json"
    legacy.write_text(json.dumps({"session_id": session_id}))
    os.utime(legacy, (mtime, mtime))
    return session_id


def _two_topics(fakes: Fakes, clock: FakeClock, sessions: SessionPool) -> tuple[str, str]:
    assert sessions.deliver("topic:1", "wake one", None, float("inf"))
    clock.advance(10)
    assert sessions.deliver("topic:2", "wake two", None, float("inf"))
    mapped = _map(fakes)
    return mapped["topic:1"], mapped["topic:2"]


def test_same_key_reuses_its_session(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)

    assert sessions.deliver("topic:1", "first", None, float("inf"))
    assert sessions.deliver("topic:1", "second", None, float("inf"))

    (session_id,) = _live(box)
    messages = _messages(box, session_id)
    assert len(messages) == 2 and messages[1] == "second", f"second wake not sent to the session: {messages}"


def test_new_topic_session_gets_the_rebuild_prompt(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)

    sessions.deliver("topic:7", "wake prompt", None, float("inf"))
    sessions.deliver("topic:7", "later prompt", None, float("inf"))

    first, later = _messages(box, _map(box)["topic:7"])
    assert first.startswith("wake prompt") and "This is a new session for thread 7" in first, first
    assert "`agent-board read 7 --last 30`" in first, f"no bounded rebuild instruction for thread 7: {first}"
    assert "`agent-board read 7`" not in first, f"the rebuild still asks for the whole thread first: {first}"
    assert later == "later prompt", f"the rebuild prompt was repeated: {later}"


def test_busy_session_leaves_delivery_undone(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)
    sessions.deliver("topic:1", "first", None, float("inf"))
    _update_sessions(box, lambda entry: entry["agentState"].update(pendingRequestsCount=1))

    delivered = sessions.deliver("topic:1", "second", None, float("inf"))

    assert delivered is False, "a busy session took a delivery"
    (session_id,) = _live(box)
    assert len(_messages(box, session_id)) == 1, "a busy session was sent the wake"


def test_no_key_always_opens_a_fresh_session(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)

    assert sessions.deliver(None, "timer one", None, float("inf"))
    assert sessions.deliver(None, "timer two", None, float("inf"))

    assert [entry["messages"] for entry in _sessions(box)] == [["timer one"], ["timer two"]]
    path = ensure_state_dir(_KIND) / "runner-sessions.json"
    recorded = json.loads(path.read_text())["sessions"]
    assert [(entry["key"], entry["retire_when_idle"]) for entry in recorded] == [(None, True), (None, True)]


def test_at_cap_evicts_least_recently_delivered_idle_session(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock, max_sessions=2)
    topic_1, topic_2 = _two_topics(box, clock, sessions)
    clock.advance(200)

    assert sessions.deliver("topic:3", "wake three", None, float("inf"))

    assert _archived(box) == [topic_1], f"wrong session evicted: {_archived(box)}"
    new = [session_id for session_id in _live(box) if session_id != topic_2]
    assert len(new) == 1, f"expected one new session: {new}"
    (prompt,) = _messages(box, new[0])
    assert prompt.startswith("wake three") and "`agent-board read 3 --last 30`" in prompt, prompt
    assert set(_map(box)) == {"topic:2", "topic:3"}, _map(box)


def test_at_cap_with_every_turn_running_leaves_delivery_undone(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock, max_sessions=2)
    topic_1, topic_2 = _two_topics(box, clock, sessions)
    _update_sessions(box, lambda entry: entry.update(turnRunning=True))
    clock.advance(200)

    delivered = sessions.deliver("topic:3", "wake three", None, float("inf"))

    assert delivered is False, "delivered past the cap with no idle session"
    assert _live(box) == [topic_1, topic_2] and _archived(box) == [], _sessions(box)
    assert set(_map(box)) == {"topic:1", "topic:2"}, _map(box)


def test_eviction_past_the_deadline_leaves_delivery_undone(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock, max_sessions=2)
    topic_1, topic_2 = _two_topics(box, clock, sessions)
    clock.advance(200)

    delivered = sessions.deliver("topic:3", "wake three", None, clock.now - 1)

    assert delivered is False, "delivered after the lease deadline"
    assert _archived(box) == [topic_1] and _live(box) == [topic_2], _sessions(box)
    assert set(_map(box)) == {"topic:2"}, f"the freed slot was not kept free: {_map(box)}"


def test_retiring_session_is_archived_once_idle_and_old_enough(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)
    sessions.deliver(None, "timer", None, float("inf"))
    clock.advance(90)

    sessions.housekeep()

    assert len(_archived(box)) == 1 and _live(box) == [], _sessions(box)
    assert _map(box) == {}, _map(box)


def test_retiring_session_younger_than_the_minimum_age_survives(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)
    sessions.deliver(None, "timer", None, float("inf"))
    clock.advance(89)

    sessions.housekeep()

    assert _archived(box) == [], _sessions(box)
    assert list(_map(box)) == [None], _map(box)


def test_topic_session_is_archived_after_idle_ttl(
    box: Fakes, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    sessions = pool(clock, ttl=600)
    topic_1, topic_2 = _two_topics(box, clock, sessions)
    clock.advance(595)

    with caplog.at_level(logging.INFO):
        sessions.housekeep()

    assert _archived(box) == [topic_1] and _live(box) == [topic_2], _sessions(box)
    assert set(_map(box)) == {"topic:2"}, _map(box)
    expected = f"{_ME.principal}: archived session {topic_1} for topic:1 (idle ttl)"
    assert expected in caplog.messages, caplog.messages


def test_running_turn_is_never_archived(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock, ttl=600)
    sessions.deliver(None, "timer", None, float("inf"))
    sessions.deliver("topic:1", "wake", None, float("inf"))
    _update_sessions(box, lambda entry: entry.update(turnRunning=True))
    clock.advance(10_000)

    sessions.housekeep()

    assert _archived(box) == [], _sessions(box)
    assert set(_map(box)) == {None, "topic:1"}, _map(box)


def test_kind_scope_never_evicts_or_reaps_on_ttl(box: Fakes, clock: FakeClock) -> None:
    pool(clock, scope="thread").deliver("topic:1", "left from thread scope", None, float("inf"))
    clock.advance(200)
    sessions = pool(clock, scope="kind", max_sessions=1, ttl=600)

    assert sessions.deliver("kind", "first", None, float("inf"))
    evicted = _archived(box)
    clock.advance(10_000)
    sessions.housekeep()

    assert evicted == [], f"kind scope evicted at the cap: {evicted}"
    kind_session = _map(box)["kind"]
    assert _live(box) == [kind_session], f"kind session reaped on ttl: {_sessions(box)}"


def test_legacy_file_becomes_the_kind_session_in_kind_scope(box: Fakes, clock: FakeClock) -> None:
    legacy = _legacy_session(box, clock.now - 10_000)
    sessions = pool(clock, scope="kind")

    sessions.housekeep()
    assert sessions.deliver("kind", "wake", None, float("inf"))

    assert _live(box) == [legacy], f"the legacy session was not kept: {_sessions(box)}"
    assert _messages(box, legacy) == ["hello", "wake"], _messages(box, legacy)
    assert _map(box) == {"kind": legacy}, _map(box)
    state = ensure_state_dir(_KIND)
    assert not (state / "runner-session.json").exists() and (state / "runner-session.json.retired").exists()


def test_legacy_file_is_retired_in_thread_scope(
    box: Fakes, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _legacy_session(box, clock.now - 100)
    sessions = pool(clock)

    with caplog.at_level(logging.INFO):
        sessions.housekeep()

    assert _archived(box) == [legacy], _sessions(box)
    assert _map(box) == {}, _map(box)
    state = ensure_state_dir(_KIND)
    assert not (state / "runner-session.json").exists() and (state / "runner-session.json.retired").exists()
    assert f"{_ME.principal}: retired legacy session {legacy}" in caplog.messages, caplog.messages


@pytest.mark.parametrize(
    ("before", "key", "after"),
    [("thread", "topic:1", "kind"), ("thread", None, "kind"), ("kind", "kind", "thread")],
)
def test_switching_scope_retires_foreign_entries(
    box: Fakes, clock: FakeClock, before: str, key: str | None, after: str
) -> None:
    pool(clock, scope=before).deliver(key, "wake", None, float("inf"))
    clock.advance(90)

    pool(clock, scope=after).housekeep()

    assert len(_archived(box)) == 1 and _live(box) == [], _sessions(box)
    assert _map(box) == {}, _map(box)


def test_corrupt_map_is_set_aside_and_the_human_warned(
    box: Fakes, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    corrupt = '{"v": 1, "sessions": [{"key": "topic:1", "session_id": "cm-lost", "last_del'
    (ensure_state_dir(_KIND) / "runner-sessions.json").write_text(corrupt)
    sessions = pool(clock)

    with caplog.at_level(logging.INFO):
        assert sessions.deliver("topic:1", "wake", None, float("inf"))
    sessions.deliver("topic:1", "again", None, float("inf"))

    set_aside = ensure_state_dir(_KIND) / f"runner-sessions.json.corrupt-{int(clock.now)}"
    assert set_aside.read_text() == corrupt, "the corrupt map was not kept for inspection"
    assert list(_map(box)) == ["topic:1"], _map(box)
    notifications = _happier(box)["notifications"]
    assert len(notifications) == 1 and "runner-sessions.json" in notifications[0]["message"], notifications
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("cm-lost" in warning for warning in warnings), warnings


def test_map_survives_a_new_pool(box: Fakes, clock: FakeClock) -> None:
    pool(clock).deliver("topic:1", "first", None, float("inf"))

    pool(clock).deliver("topic:1", "second", None, float("inf"))

    (session_id,) = _live(box)
    assert len(_messages(box, session_id)) == 2, _sessions(box)


def test_remote_control_fallback_is_kept_and_not_recorded(
    box: Fakes, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "create:spawn_failed")
    sessions = pool(clock, fallback="remote-control")

    assert sessions.deliver("topic:1", "wake", None, float("inf"))

    started = [call for call in _tmux_calls(box) if call[:1] == ["new-session"]]
    assert [call[3] for call in started] == ["board-claude"], f"wrong tmux calls: {started}"
    assert _map(box) == {}, f"the tmux session was recorded: {_map(box)}"


def test_wake_test_session_is_retired_in_thread_scope(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)

    opened = sessions.open_for_wake_test("greeting")
    assert _map(box) == {None: opened.id}, _map(box)
    clock.advance(90)
    sessions.housekeep()

    assert _archived(box) == [opened.id], _sessions(box)


def test_sessions_use_session_home(box: Fakes, clock: FakeClock, tmp_path: Path) -> None:
    session_home = tmp_path / "happier-board"
    sessions = pool(clock, happier=Happier(session_home=session_home))

    sessions.deliver("topic:1", "first", None, float("inf"))
    sessions.deliver("topic:1", "second", None, float("inf"))

    assert [entry["homes"] for entry in _sessions(box)] == [[str(session_home)] * 2], _sessions(box)


def test_delivery_names_the_session_it_used(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)
    used: list[str] = []

    assert sessions.deliver("topic:1", "wake one", None, float("inf"), on_sent=used.append)
    clock.advance(10)
    assert sessions.deliver("topic:1", "wake two", None, float("inf"), on_sent=used.append)

    assert used == [_map(box)["topic:1"]] * 2, f"sent sessions not reported: {used} vs {_map(box)}"


def test_a_delivery_left_leased_names_no_session(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock, max_sessions=1)
    used: list[str] = []
    assert sessions.deliver("topic:1", "wake one", None, float("inf"))
    _update_sessions(box, lambda entry: entry.update(turnRunning=True))

    delivered = sessions.deliver("topic:2", "wake two", None, float("inf"), on_sent=used.append)

    assert not delivered and used == [], f"a wake left leased was reported as sent: {used}"


def _consumer(fakes: Fakes, session_id: str) -> str:
    consumer = next(
        entry["env"]["AGENT_BOARD_CONSUMER"]
        for entry in _sessions(fakes)
        if entry["session"]["id"] == session_id
    )
    assert isinstance(consumer, str)
    return consumer


def test_each_new_session_gets_its_own_reading_consumer(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)

    assert sessions.deliver("topic:1", "wake one", None, float("inf"))
    assert sessions.deliver("topic:2", "wake two", None, float("inf"))

    consumers = {_consumer(box, session_id) for session_id in _live(box)}
    assert len(consumers) == 2 and all(len(consumer) == 32 for consumer in consumers)


def test_a_replacement_session_for_a_thread_takes_over_its_read_cursors(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)
    assert sessions.deliver("topic:1", "first", None, float("inf"))
    first = _map(box)["topic:1"]
    old = _consumer(box, first)
    directory = ensure_state_dir(_KIND) / CURSOR_DIR
    with locked(directory, old) as positions:
        positions.advance(1, 120)
    _update_sessions(box, lambda entry: entry["session"].update(active=False))

    assert sessions.deliver("topic:1", "second", None, float("inf"))

    second = _map(box)["topic:1"]
    new = _consumer(box, second)
    assert second != first and new != old
    with locked(directory, new) as positions:
        assert positions.get(1) == 120
    with locked(directory, old) as positions:
        assert positions.retired, "the replaced session could still advance"


def test_a_one_shot_session_inherits_no_cursor(box: Fakes, clock: FakeClock) -> None:
    sessions = pool(clock)
    assert sessions.deliver("topic:1", "first", None, float("inf"))
    old = _consumer(box, _map(box)["topic:1"])
    with locked(ensure_state_dir(_KIND) / CURSOR_DIR, old) as positions:
        positions.advance(1, 5)

    assert sessions.deliver(None, "one shot", None, float("inf"))

    one_shot = next(session for session in _live(box) if session != _map(box)["topic:1"])
    with locked(ensure_state_dir(_KIND) / CURSOR_DIR, _consumer(box, one_shot)) as positions:
        assert positions.get(1) is None
    with locked(ensure_state_dir(_KIND) / CURSOR_DIR, old) as positions:
        assert not positions.retired


def _builder(cursor: bool) -> str:
    return "cursor prompt" if cursor else "legacy prompt"


def test_the_prompt_form_follows_whether_the_receiving_session_has_a_consumer(
    box: Fakes, clock: FakeClock
) -> None:
    sessions = pool(clock)
    assert sessions.deliver("topic:1", _builder, None, float("inf"))
    session_id = _map(box)["topic:1"]
    assert _messages(box, session_id)[0].startswith("cursor prompt"), "a new session was not offered --new"

    assert sessions.deliver("topic:1", _builder, None, float("inf"))
    assert _messages(box, session_id)[-1] == "cursor prompt"

    path = ensure_state_dir(_KIND) / "runner-sessions.json"
    recorded = json.loads(path.read_text())
    for entry in recorded["sessions"]:
        entry.pop("consumer", None)
    path.write_text(json.dumps(recorded))

    assert sessions.deliver("topic:1", _builder, None, float("inf"))
    assert _messages(box, session_id)[-1] == "legacy prompt", "a session with no consumer was offered --new"


def test_a_remote_control_fallback_is_offered_only_the_stateless_read(
    box: Fakes, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "create:spawn_failed")
    started: list[str] = []
    monkeypatch.setattr(remote_control, "serves", lambda kind: True)
    monkeypatch.setattr(remote_control, "start", lambda workdir, name, prompt: started.append(prompt))
    sessions = pool(clock, fallback="remote-control")

    assert sessions.deliver("topic:1", _builder, None, float("inf"))

    assert len(started) == 1 and started[0].startswith("legacy prompt"), (
        f"the fallback was offered --new: {started}"
    )
