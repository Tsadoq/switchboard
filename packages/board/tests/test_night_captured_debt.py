from collections.abc import Iterator
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from board.db import Database, connect, migrate
from board.night import NightAdmission, admitted_for_box_in_transaction, window_on
from board.night_delivery import capture_candidates, persist_capture

pytestmark = pytest.mark.anyio
PRINCIPAL = "claude@huginn"
NIGHT = window_on(date(2026, 11, 22))
NOW = NIGHT.end - 90


@pytest.fixture
def path(tmp_path: Path) -> Iterator[Path]:
    database = tmp_path / "board.db"
    with closing(connect(database)) as conn:
        migrate(conn)
    yield database


async def _finished_capture(path: Path, principal: str = PRINCIPAL, receipt: str = "receipt-1") -> int:
    admission = NightAdmission(Database(path))
    await admission.enroll(principal, True, now=NOW)
    await admission.set_enabled(True, now=NOW)
    box = principal.partition("@")[2]
    with closing(connect(path)) as conn:
        (first,) = admitted_for_box_in_transaction(conn, box, NOW)
        conn.commit()
    first = await admission.start(principal, first.id, first.fence, NOW + 0.5)
    with closing(connect(path)) as conn:
        batch = capture_candidates(conn, first, NOW + 1)
        assert [wake["reason"] for wake in batch] == ["night_initial"]
        conn.execute(
            "INSERT INTO night_verifier_receipts (receipt_id, box, journal_id, digest, response, created_at) "
            "VALUES (?, ?, 'journal', 'digest', '{}', ?)",
            (receipt, box, NOW + 1),
        )
        persist_capture(conn, receipt, first.id, NOW + 1, batch)
        reserved = conn.execute("SELECT reserved FROM night_runs WHERE id = ?", (first.id,)).fetchone()[0]
        conn.commit()
    settled = await admission.settle(principal, first.id, first.fence, elapsed=reserved, stopped=True)
    assert settled.state == "finished"
    return int(batch[0]["id"])


def _fresh_wake(path: Path, principal: str = PRINCIPAL) -> int:
    with closing(connect(path)) as conn:
        wake = conn.execute(
            "INSERT INTO wakes (principal, reason, source_ref, summary, created_at, origin_kind) "
            "VALUES (?, 'timer', ?, 'a new task', ?, 'budgeted')",
            (principal, f"profile:{principal}", NOW + 30),
        ).lastrowid
        conn.commit()
    assert wake is not None
    return wake


async def test_a_finished_capture_alone_admits_no_further_run(path: Path) -> None:
    wake = await _finished_capture(path)

    with closing(connect(path)) as conn:
        assert admitted_for_box_in_transaction(conn, "huginn", NOW + 62) == []
        assert conn.execute("SELECT acked_at FROM wakes WHERE id = ?", (wake,)).fetchone()[0] is None
        held = conn.execute("SELECT COUNT(*) FROM night_held_wakes WHERE wake_id = ?", (wake,)).fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM night_runs").fetchone()[0]
    assert held == 1, "the captured wake stopped being held debt"
    assert runs == 1


async def test_fresh_work_still_admits_a_run_that_never_gets_the_captured_wake(path: Path) -> None:
    captured = await _finished_capture(path)
    fresh = _fresh_wake(path)

    with closing(connect(path)) as conn:
        (second,) = admitted_for_box_in_transaction(conn, "huginn", NOW + 62)
        batch = capture_candidates(conn, second, NOW + 62)

    assert [wake["id"] for wake in batch] == [fresh], "a captured wake was handed to another run"
    assert captured != fresh


async def test_another_principals_captured_debt_does_not_affect_this_one(path: Path) -> None:
    await _finished_capture(path, "claude@huginn", "receipt-1")
    admission = NightAdmission(Database(path))
    await admission.enroll("pi@muninn", True, now=NOW)
    fresh = _fresh_wake(path, "pi@muninn")

    with closing(connect(path)) as conn:
        (run,) = admitted_for_box_in_transaction(conn, "muninn", NOW + 62)
        batch = capture_candidates(conn, run, NOW + 62)

    assert run.principal == "pi@muninn"
    assert fresh in [wake["id"] for wake in batch]
