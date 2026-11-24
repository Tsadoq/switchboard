import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from typing import Any

import pytest
from board.app import create_app
from board.settings import Settings
from board.wakes import scheduler
from board.wakes.bus import WakeBus
from board.wakes.scheduler import QUIET_SECONDS, QUIET_TICKET
from fastapi import FastAPI

pytestmark = pytest.mark.anyio

NOW = 1_800_000_000.0
AGENT = "claude@testbox"
OTHER = "pi@testbox"


@asynccontextmanager
async def _board(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


def _ticket(
    settings: Settings,
    ticket_id: int,
    *,
    column: str = "doing",
    assignee: str | None = AGENT,
    touched: float = NOW - 2 * QUIET_SECONDS,
) -> int:
    thread_id = 1000 + ticket_id
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, subforum, title, kind, created_by, created_at) "
            "VALUES (?, 'tickets', ?, 'ticket', ?, ?)",
            (thread_id, f"ticket {ticket_id}", AGENT, touched),
        )
        conn.execute(
            'INSERT INTO tickets (id, title, "column", assignee, thread_id, created_by, created_at, updated_at) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, f"ticket {ticket_id}", column, assignee, thread_id, AGENT, touched, touched),
        )
    return thread_id


def _post(settings: Settings, thread_id: int, author: str, at: float) -> None:
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO posts (thread_id, author, author_kind, body, created_at) VALUES (?, ?, 'agent', 'x', ?)",
            (thread_id, author, at),
        )


async def _tick(app: FastAPI, now: float = NOW) -> None:
    await scheduler.tick(app.state.db, app.state.wakes, now)


async def _quiet_wakes(app: FastAPI, principal: str = AGENT) -> list[Any]:
    bus: WakeBus = app.state.wakes
    return [wake for wake in await bus.lease(principal) if wake.reason == QUIET_TICKET]


def _ack_all(settings: Settings) -> None:
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE wakes SET acked_at = ? WHERE acked_at IS NULL", (NOW,))


async def test_a_quiet_doing_ticket_wakes_its_assignee_once_on_its_thread(settings: Settings) -> None:
    async with _board(settings) as app:
        thread_id = _ticket(settings, 7)

        await _tick(app)
        await _tick(app)

        (wake,) = await _quiet_wakes(app)
    assert (wake.thread_id, wake.source_ref) == (thread_id, "ticket:7")
    assert '#7 "ticket 7" is in doing' in wake.summary and "move it out of doing" in wake.summary


async def test_a_recent_post_by_the_assignee_keeps_the_ticket_quiet_free(settings: Settings) -> None:
    async with _board(settings) as app:
        thread_id = _ticket(settings, 7)
        _post(settings, thread_id, AGENT, NOW - QUIET_SECONDS + 60)

        await _tick(app)

        assert await _quiet_wakes(app) == []


async def test_posts_by_others_do_not_count_as_the_assignees_work(settings: Settings) -> None:
    async with _board(settings) as app:
        thread_id = _ticket(settings, 7)
        _post(settings, thread_id, OTHER, NOW - 60)

        await _tick(app)

        assert len(await _quiet_wakes(app)) == 1


async def test_a_recent_claim_or_move_counts_as_activity(settings: Settings) -> None:
    async with _board(settings) as app:
        _ticket(settings, 7, touched=NOW - 60)

        await _tick(app)

        assert await _quiet_wakes(app) == []


@pytest.mark.parametrize(
    ("column", "assignee"),
    [("todo", AGENT), ("review", AGENT), ("done", AGENT), ("doing", None), ("doing", "human")],
)
async def test_only_doing_tickets_held_by_an_agent_are_nudged(
    settings: Settings, column: str, assignee: str | None
) -> None:
    async with _board(settings) as app:
        _ticket(settings, 7, column=column, assignee=assignee)

        await _tick(app)

        assert await _quiet_wakes(app, assignee or AGENT) == []


async def test_an_unacknowledged_nudge_is_not_repeated(settings: Settings) -> None:
    async with _board(settings) as app:
        _ticket(settings, 7)
        await _tick(app)

        await _tick(app, NOW + 2 * QUIET_SECONDS)

        assert len(await _quiet_wakes(app)) == 1


async def test_a_handled_nudge_repeats_only_after_another_quiet_period(settings: Settings) -> None:
    async with _board(settings) as app:
        _ticket(settings, 7)
        await _tick(app)
        _ack_all(settings)

        await _tick(app, NOW + QUIET_SECONDS - 1)
        assert await _quiet_wakes(app) == []

        await _tick(app, NOW + QUIET_SECONDS)
        assert len(await _quiet_wakes(app)) == 1


async def test_a_principal_whose_night_is_holding_is_not_nudged(settings: Settings) -> None:
    async with _board(settings) as app:
        _ticket(settings, 7)
        with closing(sqlite3.connect(settings.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO night_routing_days (principal, local_date, starts_at, ends_at) "
                "VALUES (?, '2027-01-15', ?, ?)",
                (AGENT, NOW - 60, NOW + 3600),
            )

        await _tick(app)
        assert await _quiet_wakes(app) == []

        await _tick(app, NOW + 3600)
        assert len(await _quiet_wakes(app)) == 1


async def test_a_failed_stamp_rolls_back_its_wake_and_the_next_tick_nudges_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _board(settings) as app:
        _ticket(settings, 7)
        stamp = scheduler._stamp_quiet

        def fail(conn: sqlite3.Connection, ticket_id: int, now: float) -> None:
            raise sqlite3.OperationalError("stamp failed")

        monkeypatch.setattr(scheduler, "_stamp_quiet", fail)
        await _tick(app)
        assert _quiet_count(settings) == 0, "a wake was kept without its stamp"

        monkeypatch.setattr(scheduler, "_stamp_quiet", stamp)
        await _tick(app, NOW + 60)
        _ack_all(settings)
        await _tick(app, NOW + 120)

        assert _quiet_count(settings) == 1


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE tickets SET \"column\" = 'done' WHERE id = 7",
        "UPDATE tickets SET assignee = 'pi@testbox' WHERE id = 7",
        "UPDATE tickets SET updated_at = 1800000000.0 WHERE id = 7",
    ],
    ids=["done", "reassigned", "moved"],
)
async def test_a_ticket_that_changes_after_selection_is_checked_again_before_its_wake(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async with _board(settings) as app:
        _ticket(settings, 7)
        select = scheduler._quiet_tickets

        def change_after_selecting(
            conn: sqlite3.Connection, now: float, only: int | None = None
        ) -> list[Any]:
            selected = select(conn, now, only)
            if only is None:
                conn.execute(change)
            return selected

        monkeypatch.setattr(scheduler, "_quiet_tickets", change_after_selecting)
        await _tick(app)

    assert _quiet_count(settings) == 0, "the old owner was nudged about a ticket that changed"


def _quiet_count(settings: Settings, principal: str | None = AGENT) -> int:
    with closing(sqlite3.connect(settings.db_path)) as conn:
        count: int = conn.execute(
            "SELECT COUNT(*) FROM wakes WHERE reason = ? AND (? IS NULL OR principal = ?)",
            (QUIET_TICKET, principal, principal),
        ).fetchone()[0]
    return count
