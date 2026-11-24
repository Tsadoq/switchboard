import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.identity import Identity
from agent_board.runner.config import KindConfig
from agent_board.runner.happier import Happier
from agent_board.runner.reaper import ORPHAN_GRACE_SECONDS, ORPHAN_HARD_LIMIT_SECONDS, OrphanReaper
from agent_board.runner.sessions import SessionPool

if TYPE_CHECKING:
    from .conftest import FakeSign


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def happier_state(fake_sign: "FakeSign", tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "happier-state.json"
    path.write_text("")
    monkeypatch.setenv("FAKE_HAPPIER_STATE", str(path))
    monkeypatch.delenv("FAKE_HAPPIER_FAIL", raising=False)
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _pool(kind: str, clock: FakeClock) -> SessionPool:
    settings = KindConfig(
        name=kind,
        workdir=Path.home() / "work",
        permission_mode="default",
        backend=f"agent:{kind}",
        fallback="none",
        session_scope="thread",
        max_sessions=2,
        idle_ttl_seconds=1800,
    )
    return SessionPool(settings, Identity(f"{kind}@testbox", kind, "testbox"), Happier(), clock)


def _entries(state: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads(state.read_text())["sessions"]
    return entries


def _update(state: Path, session_id: str, change: Callable[[dict[str, Any]], None]) -> None:
    current = json.loads(state.read_text())
    for entry in current["sessions"]:
        if entry["session"]["id"] == session_id:
            change(entry)
    state.write_text(json.dumps(current))


def _tracked(state: Path) -> list[str]:
    return [entry["session"]["id"] for entry in _entries(state) if entry.get("tracked")]


def _archived(state: Path) -> list[str]:
    return [entry["session"]["id"] for entry in _entries(state) if entry["session"].get("archivedAt")]


def _orphan(state: Path, clock: FakeClock, pool: SessionPool, key: str) -> str:
    """The muninn leak: the relay shows the session inactive, so the pool forgets it, but its child keeps running."""
    pool.deliver(key, "wake", None, float("inf"))
    session_id = _entries(state)[-1]["session"]["id"]
    _update(state, session_id, lambda entry: entry["session"].update(active=False))
    pool.housekeep()
    return str(session_id)


def test_a_session_the_pool_forgets_keeps_its_daemon_child(happier_state: Path, clock: FakeClock) -> None:
    pool = _pool("claude", clock)

    orphan = _orphan(happier_state, clock, pool, "topic:1")

    assert orphan in _tracked(happier_state), "the leak this reaper exists for no longer happens"


def test_an_orphan_is_stopped_and_archived_once_past_the_grace(
    happier_state: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, clock, pool, "topic:1")
    reaper = OrphanReaper(Happier(), [pool], clock)

    reaper.sweep()
    clock.advance(ORPHAN_GRACE_SECONDS - 1)
    reaper.sweep()
    assert orphan in _tracked(happier_state), "an orphan was stopped inside the grace"

    clock.advance(1)
    with caplog.at_level(logging.INFO):
        reaper.sweep()

    assert _tracked(happier_state) == [], _entries(happier_state)
    assert orphan in _archived(happier_state)
    assert any(f"stopped orphaned session {orphan}" in message for message in caplog.messages), (
        caplog.messages
    )


def test_sessions_any_served_kind_records_are_never_touched(happier_state: Path, clock: FakeClock) -> None:
    claude, pi = _pool("claude", clock), _pool("pi", clock)
    claude.deliver("topic:1", "wake", None, float("inf"))
    pi.deliver("topic:2", "wake", None, float("inf"))
    reaper = OrphanReaper(Happier(), [claude, pi], clock)

    reaper.sweep()
    clock.advance(ORPHAN_HARD_LIMIT_SECONDS * 2)
    reaper.sweep()

    assert len(_tracked(happier_state)) == 2 and _archived(happier_state) == [], _entries(happier_state)


def test_an_orphan_mid_turn_waits_until_the_hard_limit(happier_state: Path, clock: FakeClock) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, clock, pool, "topic:1")
    _update(happier_state, orphan, lambda entry: entry.update(turnRunning=True))
    reaper = OrphanReaper(Happier(), [pool], clock)

    reaper.sweep()
    clock.advance(ORPHAN_HARD_LIMIT_SECONDS - 1)
    reaper.sweep()
    assert orphan in _tracked(happier_state), "a running turn was cut before the hard limit"

    clock.advance(1)
    reaper.sweep()
    assert _tracked(happier_state) == [], _entries(happier_state)


def test_children_the_daemon_did_not_start_are_left_alone(happier_state: Path, clock: FakeClock) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, clock, pool, "topic:1")
    _update(happier_state, orphan, lambda entry: entry.update(startedBy="terminal"))
    reaper = OrphanReaper(Happier(), [pool], clock)

    reaper.sweep()
    clock.advance(ORPHAN_HARD_LIMIT_SECONDS)
    reaper.sweep()

    assert _tracked(happier_state) == [orphan]


def test_an_unreadable_daemon_list_stops_nothing(
    happier_state: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, clock, pool, "topic:1")
    reaper = OrphanReaper(Happier(), [pool], clock)
    reaper.sweep()
    clock.advance(ORPHAN_HARD_LIMIT_SECONDS)
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "daemon-list:no_daemon")

    with caplog.at_level(logging.WARNING):
        reaper.sweep()

    assert _tracked(happier_state) == [orphan]
    assert any("could not list the daemon's sessions" in message for message in caplog.messages), (
        caplog.messages
    )


def test_the_grace_restarts_for_a_session_that_stopped_being_an_orphan(
    happier_state: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, clock, pool, "topic:1")
    reaper = OrphanReaper(Happier(), [pool], clock)
    reaper.sweep()
    _update(happier_state, orphan, lambda entry: entry.update(tracked=False))
    clock.advance(ORPHAN_GRACE_SECONDS)
    reaper.sweep()
    _update(happier_state, orphan, lambda entry: entry.update(tracked=True))

    reaper.sweep()

    assert _tracked(happier_state) == [orphan], "an orphan was stopped without a fresh grace"


def test_a_damaged_map_stops_the_sweep_instead_of_reading_as_empty(
    happier_state: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    pool.deliver("topic:1", "wake", None, float("inf"))
    recorded = _tracked(happier_state)
    map_file = Path.home() / ".agents/board/claude/runner-sessions.json"
    reaper = OrphanReaper(Happier(), [pool], clock)
    reaper.sweep()
    map_file.write_text("{not json")
    clock.advance(ORPHAN_HARD_LIMIT_SECONDS)

    with caplog.at_level(logging.WARNING):
        reaper.sweep()

    assert _tracked(happier_state) == recorded
    assert map_file.read_text() == "{not json", "the sweep moved or rewrote the damaged map"
    assert any("session map is unreadable" in message for message in caplog.messages), caplog.messages


def test_a_stop_that_leaves_the_child_running_is_retried_and_reported(
    happier_state: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, clock, pool, "topic:1")
    reaper = OrphanReaper(Happier(), [pool], clock)
    reaper.sweep()
    clock.advance(ORPHAN_GRACE_SECONDS)
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "daemon-stop-session:refused")

    with caplog.at_level(logging.WARNING):
        reaper.sweep()
    assert orphan in _tracked(happier_state)
    assert any(f"did not stop orphaned session {orphan}" in message for message in caplog.messages)

    monkeypatch.delenv("FAKE_HAPPIER_FAIL")
    reaper.sweep()
    assert _tracked(happier_state) == []


def test_a_child_still_listed_after_a_stop_is_reported(
    happier_state: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, clock, pool, "topic:1")
    reaper = OrphanReaper(Happier(), [pool], clock)
    reaper.sweep()
    clock.advance(ORPHAN_GRACE_SECONDS)
    reaper.sweep()
    _update(happier_state, orphan, lambda entry: entry.update(tracked=True))

    with caplog.at_level(logging.WARNING):
        reaper.sweep()

    assert any(f"{orphan} is still a daemon child after it was stopped" in m for m in caplog.messages)


def test_a_session_named_only_by_the_legacy_file_is_protected(happier_state: Path, clock: FakeClock) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, clock, pool, "topic:1")
    legacy = Path.home() / ".agents/board/claude/runner-session.json"
    legacy.write_text(json.dumps({"session_id": orphan}))
    reaper = OrphanReaper(Happier(), [pool], clock)

    reaper.sweep()
    clock.advance(ORPHAN_HARD_LIMIT_SECONDS)
    reaper.sweep()
    assert _tracked(happier_state) == [orphan]

    legacy.write_text("{damaged")
    reaper.sweep()
    assert _tracked(happier_state) == [orphan]


def test_sessions_a_set_aside_map_named_are_protected(happier_state: Path, clock: FakeClock) -> None:
    pool = _pool("claude", clock)
    pool.deliver("topic:1", "wake", None, float("inf"))
    lost = _tracked(happier_state)[0]
    map_file = Path.home() / ".agents/board/claude/runner-sessions.json"
    map_file.write_text(f'{{"damaged": [{{"session_id": "{lost}"')
    pool.housekeep()
    assert list(map_file.parent.glob("runner-sessions.json.corrupt-*")), "the pool did not set the map aside"
    reaper = OrphanReaper(Happier(), [pool], clock)

    reaper.sweep()
    clock.advance(ORPHAN_HARD_LIMIT_SECONDS)
    reaper.sweep()

    assert _tracked(happier_state) == [lost]


def test_a_session_recorded_while_its_grace_runs_is_never_stopped(
    happier_state: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    reaper = OrphanReaper(Happier(), [pool], clock)
    pool.deliver("topic:1", "wake", None, float("inf"))
    opening = _tracked(happier_state)[0]
    map_file = Path.home() / ".agents/board/claude/runner-sessions.json"
    persisted = map_file.read_text()
    map_file.write_text(json.dumps({"v": 1, "sessions": []}))
    reaper.sweep()
    clock.advance(ORPHAN_GRACE_SECONDS - 1)
    map_file.write_text(persisted)
    reaper.sweep()
    clock.advance(ORPHAN_HARD_LIMIT_SECONDS)

    reaper.sweep()

    assert _tracked(happier_state) == [opening]
