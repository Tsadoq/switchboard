import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.identity import Identity
from agent_board.runner.config import KindConfig
from agent_board.runner.happier import Happier
from agent_board.runner.reaper import (
    NOT_BOARD,
    ORPHAN_BUSY_ALERT_SECONDS,
    ORPHAN_GRACE_SECONDS,
    PRESENT_AT_START,
    STOP_REFUSED,
    STOP_REPORTED,
    STOP_UNCONFIRMED,
    OrphanReaper,
)
from agent_board.runner.sessions import SessionPool

if TYPE_CHECKING:
    from .conftest import FakeSign

_LONG_AFTER = 10 * ORPHAN_BUSY_ALERT_SECONDS


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


@pytest.fixture
def ledger(fake_sign: "FakeSign") -> Path:
    path = Path.home() / ".agents/board/runner-reaper.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"v": 1, "protected": {}}))
    return path


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


def _reaper(pools: list[SessionPool], ledger: Path, clock: FakeClock) -> OrphanReaper:
    return OrphanReaper(Happier(), pools, ledger, clock)


def _state(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(path.read_text())
    return state


def _update(path: Path, session_id: str, change: Callable[[dict[str, Any]], None]) -> None:
    current = _state(path)
    for entry in current["sessions"]:
        if entry["session"]["id"] == session_id:
            change(entry)
    path.write_text(json.dumps(current))


def _tracked(path: Path) -> list[str]:
    return [entry["session"]["id"] for entry in _state(path)["sessions"] if entry.get("tracked")]


def _archived(path: Path) -> list[str]:
    return [
        entry["session"]["id"] for entry in _state(path)["sessions"] if entry["session"].get("archivedAt")
    ]


def _stops(path: Path) -> list[str]:
    stops: list[str] = _state(path).get("stops", [])
    return stops


def _protected(ledger: Path) -> dict[str, str]:
    protected: dict[str, str] = json.loads(ledger.read_text())["protected"]
    return protected


def _orphan(state: Path, pool: SessionPool, key: str = "topic:1") -> str:
    """The muninn leak: the relay shows the session inactive, so the pool forgets it, but its child keeps running."""
    pool.deliver(key, "wake", None, float("inf"))
    session_id = str(_state(state)["sessions"][-1]["session"]["id"])
    _update(state, session_id, lambda entry: entry["session"].update(active=False))
    pool.housekeep()
    return session_id


def _past_grace(reaper: OrphanReaper, clock: FakeClock, seconds: float = ORPHAN_GRACE_SECONDS) -> None:
    reaper.sweep()
    clock.advance(seconds)
    reaper.sweep()


def test_a_session_the_pool_forgets_keeps_its_daemon_child(happier_state: Path, clock: FakeClock) -> None:
    orphan = _orphan(happier_state, _pool("claude", clock))

    assert orphan in _tracked(happier_state), "the leak this reaper exists for no longer happens"


def test_an_idle_orphan_is_stopped_and_archived_once_past_the_grace(
    happier_state: Path, ledger: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    reaper = _reaper([pool], ledger, clock)

    _past_grace(reaper, clock, ORPHAN_GRACE_SECONDS - 1)
    assert orphan in _tracked(happier_state), "an orphan was stopped inside the grace"

    clock.advance(1)
    with caplog.at_level(logging.INFO):
        reaper.sweep()
        reaper.sweep()

    assert _tracked(happier_state) == [] and orphan in _archived(happier_state)
    assert any(f"reported orphaned session {orphan} stopped" in message for message in caplog.messages)
    assert any(f"{orphan} is no longer listed by the daemon" in message for message in caplog.messages)


def test_the_first_sweep_protects_every_orphan_already_present(
    happier_state: Path, fake_sign: "FakeSign", clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    existing = _orphan(happier_state, pool)
    ledger = Path.home() / ".agents/board/runner-reaper.json"
    reaper = _reaper([pool], ledger, clock)

    reaper.sweep()
    later = _orphan(happier_state, pool, "topic:2")
    _past_grace(reaper, clock, _LONG_AFTER)

    assert _protected(ledger) == {existing: PRESENT_AT_START, later: STOP_REPORTED}
    assert _tracked(happier_state) == [existing] and later in _archived(happier_state)
    assert existing not in _stops(happier_state)


def test_sessions_any_served_kind_records_are_never_touched(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    claude, pi = _pool("claude", clock), _pool("pi", clock)
    claude.deliver("topic:1", "wake", None, float("inf"))
    pi.deliver("topic:2", "wake", None, float("inf"))

    _past_grace(_reaper([claude, pi], ledger, clock), clock, _LONG_AFTER)

    assert len(_tracked(happier_state)) == 2 and _stops(happier_state) == []


def test_a_busy_orphan_is_never_stopped_and_is_reported_once(
    happier_state: Path, ledger: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    _update(happier_state, orphan, lambda entry: entry.update(turnRunning=True))
    reaper = _reaper([pool], ledger, clock)

    with caplog.at_level(logging.WARNING):
        _past_grace(reaper, clock, _LONG_AFTER)
        reaper.sweep()

    assert _tracked(happier_state) == [orphan] and _stops(happier_state) == []
    reports = [message for message in caplog.messages if f"{orphan} has been busy" in message]
    assert len(reports) == 1, caplog.messages


def test_children_the_daemon_did_not_start_are_left_alone(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    _update(happier_state, orphan, lambda entry: entry.update(startedBy="terminal"))

    _past_grace(_reaper([pool], ledger, clock), clock, _LONG_AFTER)

    assert _tracked(happier_state) == [orphan] and _stops(happier_state) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda entry: entry["session"].update(tag="something-else"),
        lambda entry: entry["session"].update(path="/srv/elsewhere"),
    ],
    ids=["tag", "path"],
)
def test_a_daemon_child_the_runner_would_not_open_is_protected(
    happier_state: Path, ledger: Path, clock: FakeClock, change: Callable[[dict[str, Any]], None]
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    _update(happier_state, orphan, change)

    _past_grace(_reaper([pool], ledger, clock), clock, _LONG_AFTER)

    assert _tracked(happier_state) == [orphan] and _stops(happier_state) == []
    assert _protected(ledger) == {orphan: NOT_BOARD}


def test_a_refused_stop_is_protected_and_never_retried(
    happier_state: Path, ledger: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    reaper = _reaper([pool], ledger, clock)
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "daemon-stop-session:refused")

    _past_grace(reaper, clock)
    monkeypatch.delenv("FAKE_HAPPIER_FAIL")
    clock.advance(_LONG_AFTER)
    reaper.sweep()

    assert _stops(happier_state) == [orphan] and _tracked(happier_state) == [orphan]
    assert _protected(ledger) == {orphan: STOP_REFUSED}


def test_an_unconfirmed_stop_is_entered_and_never_retried(
    happier_state: Path, ledger: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    reaper = _reaper([pool], ledger, clock)
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "daemon-stop-session:no_daemon")

    _past_grace(reaper, clock)
    monkeypatch.delenv("FAKE_HAPPIER_FAIL")
    clock.advance(_LONG_AFTER)
    reaper.sweep()

    assert _stops(happier_state) == [] and _tracked(happier_state) == [orphan]
    assert _protected(ledger) == {orphan: STOP_UNCONFIRMED}


def test_a_child_still_listed_after_a_reported_stop_is_never_stopped_again(
    happier_state: Path, ledger: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    reaper = _reaper([pool], ledger, clock)
    _past_grace(reaper, clock)
    _update(happier_state, orphan, lambda entry: entry.update(tracked=True))

    with caplog.at_level(logging.WARNING):
        reaper.sweep()
        clock.advance(_LONG_AFTER)
        reaper.sweep()

    assert _stops(happier_state) == [orphan]
    assert _protected(ledger) == {orphan: STOP_REPORTED}
    assert any(f"{orphan} is still listed" in message for message in caplog.messages)


def test_a_stop_is_entered_before_it_is_sent_and_a_failed_write_prevents_it(
    happier_state: Path, ledger: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    reaper = _reaper([pool], ledger, clock)
    reaper.sweep()
    clock.advance(ORPHAN_GRACE_SECONDS)
    ledger.parent.chmod(0o500)
    try:
        with pytest.raises(OSError):
            reaper.sweep()
    finally:
        ledger.parent.chmod(0o700)

    assert _stops(happier_state) == [] and _tracked(happier_state) == [orphan]


class _InterruptedStop(Happier):
    def stop_child(self, session_id: str) -> Any:
        raise KeyboardInterrupt


def test_an_interrupted_stop_stays_entered_and_is_not_repeated_after_a_restart(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    interrupted = OrphanReaper(_InterruptedStop(), [pool], ledger, clock)
    interrupted.sweep()
    clock.advance(ORPHAN_GRACE_SECONDS)
    with pytest.raises(KeyboardInterrupt):
        interrupted.sweep()

    _past_grace(_reaper([pool], ledger, clock), clock, _LONG_AFTER)

    assert _stops(happier_state) == [] and _tracked(happier_state) == [orphan]
    assert _protected(ledger) == {orphan: "stop sent, outcome not recorded"}


def test_ledger_entries_survive_a_sweep_where_the_daemon_does_not_list_them(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    ledger.write_text(json.dumps({"v": 1, "protected": {orphan: "explicitly refused"}}))
    reaper = _reaper([pool], ledger, clock)
    _update(happier_state, orphan, lambda entry: entry.update(tracked=False))
    reaper.sweep()
    _update(happier_state, orphan, lambda entry: entry.update(tracked=True))

    _past_grace(reaper, clock, _LONG_AFTER)

    assert _stops(happier_state) == [] and _protected(ledger) == {orphan: "explicitly refused"}


def test_an_unreadable_ledger_stops_nothing_and_is_kept(
    happier_state: Path, ledger: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    ledger.write_text("{damaged")
    reaper = _reaper([pool], ledger, clock)

    with caplog.at_level(logging.WARNING):
        _past_grace(reaper, clock, _LONG_AFTER)

    assert _tracked(happier_state) == [orphan] and ledger.read_text() == "{damaged"
    assert any("reaper ledger is unreadable" in message for message in caplog.messages)


def test_an_unreadable_daemon_list_stops_nothing(
    happier_state: Path,
    ledger: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    reaper = _reaper([pool], ledger, clock)
    reaper.sweep()
    clock.advance(_LONG_AFTER)
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "daemon-list:no_daemon")

    with caplog.at_level(logging.WARNING):
        reaper.sweep()

    assert _tracked(happier_state) == [orphan]
    assert any("could not list the daemon's sessions" in message for message in caplog.messages)


def test_a_damaged_map_stops_the_sweep_instead_of_reading_as_empty(
    happier_state: Path, ledger: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    pool.deliver("topic:1", "wake", None, float("inf"))
    recorded = _tracked(happier_state)
    map_file = Path.home() / ".agents/board/claude/runner-sessions.json"
    reaper = _reaper([pool], ledger, clock)
    reaper.sweep()
    map_file.write_text("{not json")
    clock.advance(_LONG_AFTER)

    with caplog.at_level(logging.WARNING):
        reaper.sweep()

    assert _tracked(happier_state) == recorded
    assert map_file.read_text() == "{not json", "the sweep moved or rewrote the damaged map"
    assert any("session map is unreadable" in message for message in caplog.messages)


def test_a_session_named_only_by_the_legacy_file_is_protected(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    legacy = Path.home() / ".agents/board/claude/runner-session.json"
    legacy.write_text(json.dumps({"session_id": orphan}))
    reaper = _reaper([pool], ledger, clock)

    _past_grace(reaper, clock, _LONG_AFTER)
    assert _tracked(happier_state) == [orphan]

    legacy.write_text("{damaged")
    reaper.sweep()
    assert _tracked(happier_state) == [orphan] and _stops(happier_state) == []


def test_a_set_aside_map_naming_sessions_stops_the_sweep(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    pool.deliver("topic:1", "wake", None, float("inf"))
    lost = _tracked(happier_state)[0]
    map_file = Path.home() / ".agents/board/claude/runner-sessions.json"
    map_file.write_text(f'{{"damaged": [{{"session_id": "{lost}"')
    pool.housekeep()
    assert list(map_file.parent.glob("runner-sessions.json.corrupt-*")), "the pool did not set the map aside"

    _past_grace(_reaper([pool], ledger, clock), clock, _LONG_AFTER)

    assert _tracked(happier_state) == [lost] and _stops(happier_state) == []


def test_a_session_recorded_while_its_grace_runs_is_never_stopped(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    reaper = _reaper([pool], ledger, clock)
    pool.deliver("topic:1", "wake", None, float("inf"))
    opening = _tracked(happier_state)[0]
    map_file = Path.home() / ".agents/board/claude/runner-sessions.json"
    persisted = map_file.read_text()
    map_file.write_text(json.dumps({"v": 1, "sessions": []}))
    reaper.sweep()
    clock.advance(ORPHAN_GRACE_SECONDS - 1)
    map_file.write_text(persisted)
    reaper.sweep()
    clock.advance(_LONG_AFTER)

    reaper.sweep()

    assert _tracked(happier_state) == [opening] and _stops(happier_state) == []


def test_the_grace_restarts_for_a_session_that_stopped_being_an_orphan(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    reaper = _reaper([pool], ledger, clock)
    reaper.sweep()
    _update(happier_state, orphan, lambda entry: entry.update(tracked=False))
    clock.advance(ORPHAN_GRACE_SECONDS)
    reaper.sweep()
    _update(happier_state, orphan, lambda entry: entry.update(tracked=True))

    reaper.sweep()

    assert _tracked(happier_state) == [orphan], "an orphan was stopped without a fresh grace"


def test_a_set_aside_map_naming_no_session_still_stops_the_sweep(
    happier_state: Path, ledger: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    pool = _pool("claude", clock)
    pool.deliver("topic:1", "wake", None, float("inf"))
    resident = _tracked(happier_state)[0]
    map_file = Path.home() / ".agents/board/claude/runner-sessions.json"
    map_file.write_text("{corrupt")
    pool.housekeep()

    with caplog.at_level(logging.WARNING):
        _past_grace(_reaper([pool], ledger, clock), clock, _LONG_AFTER)

    assert _tracked(happier_state) == [resident] and _stops(happier_state) == []
    assert any("is unresolved" in message for message in caplog.messages)


class _RemapsWhileProbing(Happier):
    def __init__(self, pool: SessionPool, key: str) -> None:
        super().__init__()
        self._pool, self._key = pool, key

    def confirmed_idle(self, session_id: str, timeout: int = 3) -> bool:
        map_file = Path.home() / ".agents/board/claude/runner-sessions.json"
        current = json.loads(map_file.read_text())
        current["sessions"].append(
            {"key": self._key, "session_id": session_id, "last_delivery": 0.0, "retire_when_idle": False}
        )
        map_file.write_text(json.dumps(current))
        return True


def test_a_session_recorded_during_the_idle_probe_is_not_stopped(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    reaper = OrphanReaper(_RemapsWhileProbing(pool, "topic:9"), [pool], ledger, clock)

    _past_grace(reaper, clock)

    assert _tracked(happier_state) == [orphan] and _stops(happier_state) == []


def test_an_orphan_the_relay_has_archived_is_not_taken_as_idle(
    happier_state: Path, ledger: Path, clock: FakeClock
) -> None:
    pool = _pool("claude", clock)
    orphan = _orphan(happier_state, pool)
    Happier().archive(orphan)
    assert orphan in _tracked(happier_state), "archiving an inactive session stopped its child"

    _past_grace(_reaper([pool], ledger, clock), clock, _LONG_AFTER)

    assert _tracked(happier_state) == [orphan] and _stops(happier_state) == []
