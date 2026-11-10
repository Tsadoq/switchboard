import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from board.db import Database, connect, migrate
from board.night import AdmissionDenied, NightAdmission, NightWindow, window_for, window_on

pytestmark = pytest.mark.anyio


@pytest.fixture
def admission(tmp_path: Path) -> NightAdmission:
    path = tmp_path / "board.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return NightAdmission(Database(path))


def instant(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


async def enable(admission: NightAdmission, *principals: str) -> None:
    for principal in principals:
        await admission.enroll(principal, True)
    await admission.set_enabled(True)


async def test_dst_windows_slots_and_strict_boundaries() -> None:
    for day, hours in [(date(2026, 3, 29), 4), (date(2026, 10, 25), 6), (date(2026, 11, 11), 5)]:
        night = window_on(day)
        assert night.end - night.start == hours * 3600
        assert night.start <= night.slot("pi@dev") < night.end
        assert night.slot("pi@dev") == window_on(day).slot("pi@dev")
        assert window_for(night.start) == night
        assert window_for(night.end) is None
        assert datetime.fromtimestamp(night.start, UTC).tzinfo is UTC


async def test_disabled_default_and_excluded_hosts(admission: NightAdmission) -> None:
    now = instant("2026-11-11T00:30:00+00:00")
    with pytest.raises(AdmissionDenied, match="disabled"):
        await admission.admit("pi@dev", now)
    for principal in ("pi@ops", "claude@heimdall", "human"):
        with pytest.raises(AdmissionDenied, match="excluded"):
            await admission.enroll(principal, True)
    await enable(admission, "pi@dev")
    with pytest.raises(AdmissionDenied, match="outside_window"):
        await admission.admit("pi@dev", instant("2026-11-11T05:00:00+00:00"))


async def test_capacity_is_atomic_and_active_batches_do_not_extend(admission: NightAdmission) -> None:
    principals = ("pi@dev", "claude@dev", "pi@huginn", "pi@muninn")
    await enable(admission, *principals)
    now = instant("2026-11-11T00:30:00+00:00")
    results = await asyncio.gather(*(admission.admit(p, now) for p in principals), return_exceptions=True)
    runs = [result for result in results if not isinstance(result, BaseException)]
    assert len(runs) == 2 and len({run.box for run in runs}) == 2
    first = runs[0]
    again = await admission.admit(first.principal, now + 10)
    assert again == first
    assert first.deadline == now + 1200


async def test_budget_settlement_fencing_and_end_of_window(admission: NightAdmission) -> None:
    await enable(admission, "pi@dev")
    now = instant("2026-11-11T00:30:00+00:00")
    first = await admission.admit("pi@dev", now)
    await admission.start(first.principal, first.id, first.fence, now + 1)
    await admission.settle(first.principal, first.id, first.fence, elapsed=300, stopped=True)
    second = await admission.admit("pi@dev", now + 400)
    assert second.reserved == 900
    with pytest.raises(AdmissionDenied, match="stale_fence"):
        await admission.settle(second.principal, second.id, first.fence, elapsed=0, stopped=True)
    await admission.settle(second.principal, second.id, second.fence, elapsed=900, stopped=True)
    with pytest.raises(AdmissionDenied, match="budget"):
        await admission.admit("pi@dev", now + 1400)
    await admission.enroll("pi@huginn", True)
    last = await admission.admit("pi@huginn", instant("2026-11-11T04:59:59+00:00"))
    assert last.reserved == 1 and last.deadline == instant("2026-11-11T05:00:00+00:00")
    with pytest.raises(AdmissionDenied, match="expired"):
        await admission.start(last.principal, last.id, last.fence, last.deadline)


async def test_uncertain_termination_holds_capacity_across_nights(admission: NightAdmission) -> None:
    await enable(admission, "pi@dev", "claude@dev")
    now = instant("2026-11-11T00:30:00+00:00")
    run = await admission.admit("pi@dev", now)
    await admission.settle(run.principal, run.id, run.fence, elapsed=0, stopped=False)
    with pytest.raises(AdmissionDenied, match="capacity"):
        await admission.admit("claude@dev", now + 1300)
    with pytest.raises(AdmissionDenied, match="capacity"):
        await admission.admit("claude@dev", now + 86400)
    await admission.settle(run.principal, run.id, run.fence, elapsed=0, stopped=True)
    with pytest.raises(AdmissionDenied, match="budget"):
        await admission.admit("pi@dev", now + 1400)
    await admission.admit("claude@dev", now + 86400)


async def test_off_and_unenrollment_request_stop_without_releasing(admission: NightAdmission) -> None:
    await enable(admission, "pi@dev", "claude@huginn")
    now = instant("2026-11-11T00:30:00+00:00")
    first = await admission.admit("pi@dev", now)
    second = await admission.admit("claude@huginn", now)
    await admission.enroll(first.principal, False)
    assert (await admission.run(first.id)).state == "stopping"
    await admission.set_enabled(False)
    assert (await admission.run(second.id)).state == "stopping"
    with pytest.raises(AdmissionDenied, match="disabled"):
        await admission.start(second.principal, second.id, second.fence, now + 1)


async def test_initial_slot_claim_is_persistent_and_skips_late_catchup(admission: NightAdmission) -> None:
    await enable(admission, "pi@dev")
    night = window_on(date(2026, 11, 11))
    slot = night.slot("pi@dev")
    assert not await admission.claim_initial("pi@dev", slot - 1)
    results = await asyncio.gather(*(admission.claim_initial("pi@dev", slot) for _ in range(8)))
    assert results.count(True) == 1
    assert not await admission.claim_initial("pi@dev", slot + 1)
    tomorrow = window_on(date(2026, 11, 12))
    assert not await admission.claim_initial("pi@dev", tomorrow.slot("pi@dev") + 60)
    assert not await admission.claim_initial("pi@dev", tomorrow.end)
    assert not await admission.claim_initial("pi@dev", tomorrow.slot("pi@dev"))


async def test_restart_and_late_completion_cannot_release_another_run(
    admission: NightAdmission, tmp_path: Path
) -> None:
    await enable(admission, "pi@dev", "claude@dev")
    now = instant("2026-11-11T00:30:00+00:00")
    first = await admission.admit("pi@dev", now)
    restarted = NightAdmission(Database(tmp_path / "board.db"))
    assert await restarted.admit("pi@dev", now + 1) == first
    with pytest.raises(AdmissionDenied, match="stale_fence"):
        await restarted.settle("claude@dev", first.id, first.fence, elapsed=0, stopped=True)
    await restarted.settle(first.principal, first.id, first.fence, elapsed=100, stopped=True)
    second = await restarted.admit("pi@dev", now + 200)
    await restarted.settle(first.principal, first.id, first.fence, elapsed=100, stopped=True)
    assert await restarted.run(second.id) == second
    with pytest.raises(AdmissionDenied, match="capacity"):
        await restarted.admit("claude@dev", now + 201)
    with pytest.raises(AdmissionDenied, match="invalid_elapsed"):
        await restarted.settle(second.principal, second.id, second.fence, elapsed=-1, stopped=True)


@pytest.mark.parametrize("stopped", [False, True])
async def test_overrun_is_recorded_and_quarantine_cannot_be_cleared_by_a_retry(
    admission: NightAdmission, tmp_path: Path, stopped: bool
) -> None:
    await enable(admission, "pi@dev", "claude@dev")
    now = instant("2026-11-11T00:30:00+00:00")
    run = await admission.admit("pi@dev", now)
    overrun = await admission.settle(run.principal, run.id, run.fence, elapsed=1201, stopped=stopped)
    assert overrun.state == "quarantined" and overrun.elapsed == 1201
    retry = await admission.settle(run.principal, run.id, run.fence, elapsed=0, stopped=True)
    assert retry == overrun
    with closing(connect(tmp_path / "board.db")) as conn:
        budget = conn.execute("SELECT spent, reserved FROM night_budget").fetchone()
        assert tuple(budget) == (1200, 0)
    with pytest.raises(AdmissionDenied, match="capacity"):
        await admission.admit("claude@dev", now + 1300)


@pytest.mark.parametrize("elapsed", [float("nan"), float("inf"), float("-inf"), -1])
async def test_invalid_elapsed_never_changes_or_releases_a_run(
    admission: NightAdmission, elapsed: float
) -> None:
    await enable(admission, "pi@dev")
    run = await admission.admit("pi@dev", instant("2026-11-11T00:30:00+00:00"))
    with pytest.raises(AdmissionDenied, match="invalid_elapsed"):
        await admission.settle(run.principal, run.id, run.fence, elapsed=elapsed, stopped=True)
    assert await admission.run(run.id) == run


async def test_initial_outbox_and_claim_roll_back_together(admission: NightAdmission, tmp_path: Path) -> None:
    await enable(admission, "pi@dev")
    now = window_on(date(2026, 11, 11)).slot("pi@dev")

    def emit(conn: sqlite3.Connection, night: NightWindow) -> None:
        conn.execute(
            "INSERT INTO wakes (principal, reason, summary, created_at) VALUES ('pi@dev', 'timer', 'Test', ?)",
            (night.start,),
        )
        raise RuntimeError("outbox failed")

    with pytest.raises(RuntimeError, match="outbox failed"):
        await admission.claim_initial("pi@dev", now, emit=emit)
    with closing(connect(tmp_path / "board.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0] == 0

    def successful(conn: sqlite3.Connection, night: NightWindow) -> None:
        conn.execute(
            "INSERT INTO wakes (principal, reason, summary, created_at) VALUES ('pi@dev', 'timer', 'Test', ?)",
            (night.start,),
        )

    assert await admission.claim_initial("pi@dev", now, emit=successful)
    assert not await admission.claim_initial("pi@dev", now, emit=successful)
    with closing(connect(tmp_path / "board.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0] == 1
