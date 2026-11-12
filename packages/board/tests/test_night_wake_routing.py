import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from board.db import Database, connect, migrate
from board.forum.fanout import WakeRequest
from board.night import AdmissionDenied, NightAdmission, Run
from board.wakes.bus import WakeBus

pytestmark = pytest.mark.anyio
NOW = datetime.fromisoformat("2026-11-12T01:00:00+00:00").timestamp()
type Routing = tuple[sqlite3.Connection, WakeBus, NightAdmission]


@pytest.fixture
def routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Routing]:
    path = tmp_path / "board.db"
    with closing(connect(path)) as conn:
        migrate(conn)
        conn.execute("INSERT INTO subforums (slug, title, description) VALUES ('test', 'Test', '')")
        conn.execute(
            "INSERT INTO threads (id, subforum, title, kind, created_by, created_at) "
            "VALUES (1, 'test', 'Test', 'discussion', 'human', 0)"
        )
        for post_id, author, kind in [(1, "human", "human"), (2, "pi@muninn", "agent")]:
            conn.execute(
                "INSERT INTO posts (id, thread_id, author, author_kind, body, created_at) "
                "VALUES (?, 1, ?, ?, 'incident stop', 0)",
                (post_id, author, kind),
            )
        conn.commit()
        monkeypatch.setattr("board.wakes.bus.time.time", lambda: NOW)
        db = Database(path)
        yield conn, WakeBus(db, lease_seconds=300), NightAdmission(db)


async def mixed(bus: WakeBus, principal: str = "pi@dev") -> None:
    await bus.emit(WakeRequest(principal, "follow", "post:2", "human stop incident", 1, 1))
    await bus.emit(WakeRequest(principal, "mention", "post:1", "human", 1, 1))
    await bus.emit(WakeRequest(principal, "timer", "profile:pi@dev", "scheduled wake"))


async def test_default_unenrolled_delivery_is_unchanged(routing: Routing) -> None:
    conn, bus, _ = routing
    await mixed(bus)
    assert [wake.id for wake in await bus.lease("pi@dev")] == [1, 2, 3]
    assert conn.execute("SELECT COUNT(*) FROM night_held_wakes").fetchone()[0] == 0


async def test_mixed_batch_holds_budgeted_rows_before_sql_lease_even_with_switch_off(
    routing: Routing,
) -> None:
    conn, bus, admission = routing
    await admission.enroll("pi@dev", True, now=NOW)
    await mixed(bus)
    assert [wake.id for wake in await bus.lease("pi@dev")] == [2]
    assert await bus.lease("pi@dev") == []
    assert await bus.ack("pi@dev", [1, 2, 3]) == 1
    rows = conn.execute("SELECT id, leased_until, acked_at FROM wakes ORDER BY id").fetchall()
    assert [(row[0], row[1], row[2]) for row in (rows[0], rows[2])] == [(1, None, None), (3, None, None)]
    assert conn.execute("SELECT enabled FROM night_control").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["off", "unenroll"])
async def test_control_change_never_flushes_held_or_future_night_wakes(
    routing: Routing, operation: str
) -> None:
    conn, bus, admission = routing
    await admission.enroll("pi@dev", True, now=NOW)
    await admission.set_enabled(True, now=NOW)
    await mixed(bus)
    if operation == "off":
        await admission.set_enabled(False, now=NOW + 1)
    else:
        await admission.enroll("pi@dev", False, now=NOW + 1)
    await bus.emit(WakeRequest("pi@dev", "timer", "profile:pi@dev", "new timer"))
    assert [wake.id for wake in await bus.lease("pi@dev")] == [2]
    assert conn.execute("SELECT COUNT(*) FROM night_held_wakes").fetchone()[0] == 3


async def test_queued_before_enrollment_is_held_atomically_on_enrollment(routing: Routing) -> None:
    _, bus, admission = routing
    await mixed(bus)
    await admission.enroll("pi@dev", True, now=NOW)
    await admission.enroll("pi@dev", False, now=NOW + 1)
    assert [wake.id for wake in await bus.lease("pi@dev")] == [2]


async def test_human_work_requests_stop_and_waits_for_trusted_settlement(routing: Routing) -> None:
    _, bus, admission = routing
    await admission.enroll("pi@dev", True, now=NOW)
    await admission.set_enabled(True, now=NOW)
    run = await admission.admit("pi@dev", NOW)
    await mixed(bus)
    assert (await admission.run(run.id)).state == "stopping"
    assert await bus.lease("pi@dev") == []
    await admission.settle("pi@dev", run.id, run.fence, elapsed=0, stopped=False)
    assert await bus.lease("pi@dev") == []
    await admission.settle("pi@dev", run.id, run.fence, elapsed=0, stopped=True)
    assert [wake.id for wake in await bus.lease("pi@dev")] == [2]


async def test_restart_window_expiry_and_lease_expiry_do_not_release_holds(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, bus, admission = routing
    await admission.enroll("pi@dev", True, now=NOW)
    await mixed(bus)
    assert [wake.id for wake in await bus.lease("pi@dev")] == [2]
    monkeypatch.setattr("board.wakes.bus.time.time", lambda: NOW + 24 * 3600)
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    restarted = WakeBus(Database(path), lease_seconds=300)
    assert [wake.id for wake in await restarted.lease("pi@dev")] == [2]
    assert await restarted.ack("pi@dev", [1, 3]) == 0
    assert conn.execute("SELECT COUNT(*) FROM night_held_wakes").fetchone()[0] == 2


async def test_daytime_enrollment_does_not_hold_unbudgeted_delivery(
    routing: Routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, bus, admission = routing
    daytime = datetime.fromisoformat("2026-11-12T12:00:00+00:00").timestamp()
    monkeypatch.setattr("board.wakes.bus.time.time", lambda: daytime)
    await admission.enroll("pi@dev", True, now=daytime)
    await mixed(bus)
    assert [wake.id for wake in await bus.lease("pi@dev")] == [1, 2, 3]
    assert conn.execute("SELECT COUNT(*) FROM night_held_wakes").fetchone()[0] == 0


async def test_human_emission_and_admission_race_never_leaves_a_runnable_run(routing: Routing) -> None:
    _, bus, admission = routing
    await admission.enroll("pi@dev", True, now=NOW)
    await admission.set_enabled(True, now=NOW)
    results = await asyncio.gather(
        admission.admit("pi@dev", NOW),
        bus.emit(WakeRequest("pi@dev", "mention", "post:1", "human", 1, 1)),
        return_exceptions=True,
    )
    run = results[0]
    if isinstance(run, AdmissionDenied):
        assert str(run) == "human_priority"
    else:
        assert isinstance(run, Run)
        assert (await admission.run(run.id)).state == "stopping"


async def test_hold_does_not_block_another_unenrolled_principal(routing: Routing) -> None:
    _, bus, admission = routing
    await admission.enroll("pi@dev", True, now=NOW)
    await mixed(bus)
    await mixed(bus, "pi@huginn")
    assert [wake.id for wake in await bus.lease("pi@huginn")] == [4, 5, 6]
