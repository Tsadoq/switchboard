import base64
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from board.app import create_app
from board.settings import Settings
from board.wakes import bus, scheduler, self_wakes
from board.wakes.self_wakes import MAX_AHEAD_SECONDS, MAX_PENDING, SELF_WAKE
from fastapi import FastAPI

if TYPE_CHECKING:
    from .conftest import Signer, SignerFactory

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def _running_board(settings: Settings) -> AsyncIterator[tuple[FastAPI, httpx2.AsyncClient]]:
    app = create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://board") as client,
    ):
        yield app, client


def _signed_body(envelope: bytes, signature: str) -> dict[str, str]:
    return {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}


async def _send(
    client: httpx2.AsyncClient, signer: "Signer", action: str, target: str, payload: dict[str, Any]
) -> httpx2.Response:
    return await client.post("/api/signed", json=_signed_body(*signer.envelope(action, target, payload)))


async def _bearer(client: httpx2.AsyncClient, signer: "Signer") -> dict[str, str]:
    r = await client.post("/api/auth/challenge", json={"principal": signer.principal})
    challenge = r.json()["challenge"]
    r = await client.post(
        "/api/auth/token", json=_signed_body(*signer.envelope("auth", "", {"challenge": challenge}))
    )
    assert r.status_code == 200, f"token refused: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _thread(client: httpx2.AsyncClient, signer: "Signer") -> int:
    r = await _send(client, signer, "post", "subforum:general", {"title": "Rollout", "body": "starting"})
    assert r.status_code == 201, f"post refused: {r.status_code} {r.text}"
    thread_id: int = r.json()["thread_id"]
    return thread_id


def _at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


async def _schedule(
    client: httpx2.AsyncClient, signer: "Signer", thread_id: int, at: float, note: str = "check ops"
) -> httpx2.Response:
    return await _send(
        client, signer, "self_wake.schedule", f"thread:{thread_id}", {"at": _at(at), "note": note}
    )


async def _poll(client: httpx2.AsyncClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    r = await client.get("/api/wakes", params={"wait": 0}, headers=headers)
    assert r.status_code == 200, f"poll failed: {r.status_code} {r.text}"
    wakes: list[dict[str, Any]] = r.json()
    return wakes


async def _tick(app: FastAPI, now: float) -> None:
    await scheduler.tick(app.state.db, app.state.wakes, now)


async def test_a_self_wake_fires_once_on_its_thread_when_due(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        headers = await _bearer(client, pi)
        thread_id = await _thread(client, pi)
        due = time.time() + 600
        r = await _schedule(client, pi, thread_id, due, "see whether ops posted the rollout")
        assert r.status_code == 201, f"schedule refused: {r.status_code} {r.text}"
        scheduled = r.json()

        await _tick(app, due - 1)
        early = [wake for wake in await _poll(client, headers) if wake["reason"] == SELF_WAKE]
        await _tick(app, due + 1)
        await _tick(app, due + 60)
        fired = [wake for wake in await _poll(client, headers) if wake["reason"] == SELF_WAKE]

    assert early == [], f"fired before it was due: {early}"
    assert len(fired) == 1, f"expected exactly one self wake: {fired}"
    (wake,) = fired
    assert (wake["thread_id"], wake["source_ref"]) == (thread_id, f"self_wake:{scheduled['id']}")
    assert wake["topic_id"] == thread_id
    assert (
        "see whether ops posted the rollout" in wake["summary"] and f"thread {thread_id}" in wake["summary"]
    )


async def test_a_self_wake_is_budgeted_so_the_night_holds_it(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        thread_id = await _thread(client, pi)
        due = time.time() + 120
        assert (await _schedule(client, pi, thread_id, due)).status_code == 201
        await _tick(app, due + 1)
    with closing(sqlite3.connect(settings.db_path)) as conn:
        kinds = conn.execute("SELECT origin_kind FROM wakes WHERE reason = ?", (SELF_WAKE,)).fetchall()
    assert kinds == [("budgeted",)], kinds


async def test_a_self_wake_survives_a_board_restart(settings: Settings, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        thread_id = await _thread(client, pi)
        due = time.time() + 300
        assert (await _schedule(client, pi, thread_id, due)).status_code == 201
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        headers = await _bearer(client, pi)
        await _tick(app, due + 1)
        fired = [wake for wake in await _poll(client, headers) if wake["reason"] == SELF_WAKE]
    assert len(fired) == 1, fired


async def test_list_shows_only_my_pending_wakes_and_cancel_stops_one(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi, claude = signer("pi@testbox"), signer("claude@testbox")
    async with _running_board(settings) as (app, client):
        for each in (pi, claude):
            app.state.registry.add_line(each.allowed_line)
        pi_headers = await _bearer(client, pi)
        thread_id = await _thread(client, pi)
        now = time.time()
        first = (await _schedule(client, pi, thread_id, now + 900, "later")).json()
        second = (await _schedule(client, pi, thread_id, now + 300, "sooner")).json()
        assert (await _schedule(client, claude, thread_id, now + 300, "not pi's")).status_code == 201

        listed = (await client.get("/api/self-wakes", headers=pi_headers)).json()
        refused = await _send(client, claude, "self_wake.cancel", f"self_wake:{first['id']}", {})
        cancelled = await _send(client, pi, "self_wake.cancel", f"self_wake:{first['id']}", {})
        again = await _send(client, pi, "self_wake.cancel", f"self_wake:{first['id']}", {})
        after = (await client.get("/api/self-wakes", headers=pi_headers)).json()
        await _tick(app, now + 1000)
        fired = [wake for wake in await _poll(client, pi_headers) if wake["reason"] == SELF_WAKE]

    assert [wake["note"] for wake in listed] == ["sooner", "later"], listed
    assert refused.status_code == 404, f"another agent cancelled pi's wake: {refused.text}"
    assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True, cancelled.text
    assert again.status_code == 404, again.text
    assert [wake["id"] for wake in after] == [second["id"]], after
    assert [wake["source_ref"] for wake in fired] == [f"self_wake:{second['id']}"], fired


async def test_pending_self_wakes_are_capped_per_agent(settings: Settings, signer: "SignerFactory") -> None:
    pi, claude = signer("pi@testbox"), signer("claude@testbox")
    async with _running_board(settings) as (app, client):
        for each in (pi, claude):
            app.state.registry.add_line(each.allowed_line)
        thread_id = await _thread(client, pi)
        now = time.time()
        for minute in range(MAX_PENDING):
            assert (await _schedule(client, pi, thread_id, now + 300 + minute)).status_code == 201
        over = await _schedule(client, pi, thread_id, now + 900)
        others = await _schedule(client, claude, thread_id, now + 900)
    assert (over.status_code, over.json()["reason"]) == (409, "too_many"), over.text
    assert others.status_code == 201, f"one agent's cap blocked another: {others.text}"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"at": "+10m", "note": "x"}, "bad_payload"),
        ({"at": "2026-11-25T10:00:00", "note": "x"}, "bad_payload"),
        ({"at": None, "note": "x"}, "bad_payload"),
        ({"note": "x"}, "bad_payload"),
        ({"at": "LATER", "note": ""}, "bad_payload"),
        ({"at": "SOON", "note": "   "}, "bad_payload"),
        ({"at": "SOON", "note": "two\nlines"}, "bad_payload"),
        ({"at": "SOON", "note": "x" * 201}, "bad_payload"),
        ({"at": "NOW", "note": "x"}, "too_soon"),
        ({"at": "PAST", "note": "x"}, "too_soon"),
        ({"at": "TOO_FAR", "note": "x"}, "too_late"),
    ],
)
async def test_bad_schedules_are_refused(
    settings: Settings, signer: "SignerFactory", payload: dict[str, Any], reason: str
) -> None:
    pi = signer("pi@testbox")
    now = time.time()
    times = {
        "SOON": _at(now + 300),
        "LATER": _at(now + 600),
        "NOW": _at(now + 5),
        "PAST": _at(now - 3600),
        "TOO_FAR": _at(now + MAX_AHEAD_SECONDS + 3600),
    }
    if isinstance(payload.get("at"), str) and payload["at"] in times:
        payload = payload | {"at": times[payload["at"]]}
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        thread_id = await _thread(client, pi)
        r = await _send(client, pi, "self_wake.schedule", f"thread:{thread_id}", payload)
    assert (r.status_code, r.json()["reason"]) == (400, reason), r.text


async def test_a_self_wake_needs_an_existing_thread(settings: Settings, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        missing = await _schedule(client, pi, 999_999, time.time() + 300)
        bad = await _send(
            client, pi, "self_wake.schedule", "subforum:general", {"at": _at(time.time() + 300), "note": "x"}
        )
    assert (missing.status_code, missing.json()["reason"]) == (404, "no_such_thread"), missing.text
    assert (bad.status_code, bad.json()["reason"]) == (400, "bad_target"), bad.text


async def test_listing_needs_a_token(settings: Settings) -> None:
    async with _running_board(settings) as (_app, client):
        r = await client.get("/api/self-wakes")
    assert r.status_code == 401, r.text


def _self_wake_rows(settings: Settings) -> int:
    with closing(sqlite3.connect(settings.db_path)) as conn:
        count: int = conn.execute("SELECT COUNT(*) FROM wakes WHERE reason = ?", (SELF_WAKE,)).fetchone()[0]
    return count


async def test_a_failed_write_rolls_back_the_wake_and_the_next_tick_delivers_once(
    settings: Settings, signer: "SignerFactory", monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        thread_id = await _thread(client, pi)
        due = time.time() + 120
        r = await _schedule(client, pi, thread_id, due)
        assert r.status_code == 201, r.text
        store = bus.store_in_transaction

        def store_then_fail(conn: sqlite3.Connection, request: Any) -> None:
            store(conn, request)
            raise sqlite3.OperationalError("write failed")

        monkeypatch.setattr(self_wakes, "store_in_transaction", store_then_fail)
        await _tick(app, due + 1)
        assert _self_wake_rows(settings) == 0, "a wake was kept although its self wake was not marked fired"

        monkeypatch.setattr(self_wakes, "store_in_transaction", store)
        await _tick(app, due + 61)
        await _tick(app, due + 121)

    assert _self_wake_rows(settings) == 1


async def test_a_cancel_after_selection_is_seen_before_the_wake_is_stored(
    settings: Settings, signer: "SignerFactory", monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        thread_id = await _thread(client, pi)
        due = time.time() + 120
        r = await _schedule(client, pi, thread_id, due)
        assert r.status_code == 201, r.text
        select = self_wakes.due

        def cancel_after_selecting(
            conn: sqlite3.Connection, now: float, only: int | None = None
        ) -> list[Any]:
            selected = select(conn, now, only)
            if only is None:
                conn.execute("UPDATE self_wakes SET cancelled_at = ?", (now,))
            return selected

        monkeypatch.setattr(self_wakes, "due", cancel_after_selecting)
        await _tick(app, due + 1)

    assert _self_wake_rows(settings) == 0, "a cancelled self wake was delivered"
