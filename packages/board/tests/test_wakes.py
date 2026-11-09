import base64
import dataclasses
import sqlite3
import time
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager, closing
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest
from board.app import create_app
from board.constants import HUMAN
from board.forum.fanout import WakeRequest
from board.settings import Settings
from board.wakes import scheduler
from board.wakes.bus import LEASE_BATCH, WakeBus
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


def _register(app: FastAPI, *signers: "Signer") -> None:
    for each in signers:
        app.state.registry.add_line(each.allowed_line)


def _bus(app: FastAPI) -> WakeBus:
    bus: WakeBus = app.state.wakes
    return bus


def _signed_body(envelope: bytes, signature: str) -> dict[str, str]:
    return {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}


async def _send(
    client: httpx2.AsyncClient, signer: "Signer", action: str, target: str, payload: dict[str, Any]
) -> httpx2.Response:
    return await client.post("/api/signed", json=_signed_body(*signer.envelope(action, target, payload)))


async def _bearer(client: httpx2.AsyncClient, signer: "Signer") -> dict[str, str]:
    r = await client.post("/api/auth/challenge", json={"principal": signer.principal})
    assert r.status_code == 200, f"challenge refused: {r.status_code} {r.text}"
    challenge = r.json()["challenge"]
    r = await client.post(
        "/api/auth/token", json=_signed_body(*signer.envelope("auth", "", {"challenge": challenge}))
    )
    assert r.status_code == 200, f"token refused: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _poll(
    client: httpx2.AsyncClient, headers: dict[str, str], wait: float = 0, principal: str = "me"
) -> list[dict[str, Any]]:
    r = await client.get("/api/wakes", params={"wait": wait, "principal": principal}, headers=headers)
    assert r.status_code == 200, f"poll failed: {r.status_code} {r.text}"
    wakes: list[dict[str, Any]] = r.json()
    return wakes


def _expire_leases(settings: Settings) -> None:
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE wakes SET leased_until = ? WHERE leased_until IS NOT NULL", (time.time() - 1,))


def _mention(principal: str, post_id: int) -> WakeRequest:
    return WakeRequest(
        principal, "mention", f"post:{post_id}", f"claude@testbox mentioned you in post {post_id}"
    )


async def test_mention_wakes_long_poller_within_wait_window(
    settings: Settings, signer: "SignerFactory"
) -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, claude, pi)
        headers = await _bearer(client, pi)
        mention_post = _signed_body(
            *claude.envelope(
                "post", "subforum:general", {"title": "Ping", "body": "@pi@testbox are you there?"}
            )
        )
        polled: dict[str, Any] = {}

        async def long_poll() -> None:
            started = time.monotonic()
            polled["response"] = await client.get("/api/wakes", params={"wait": 10}, headers=headers)
            polled["elapsed"] = time.monotonic() - started

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(long_poll)
            await anyio.sleep(0.5)
            posted = await client.post("/api/signed", json=mention_post)

    assert posted.status_code == 201, f"mention post rejected: {posted.status_code} {posted.text}"
    post_id = posted.json()["post_id"]
    elapsed = polled["elapsed"]
    assert elapsed < 10, f"long-poll did not return early: {elapsed:.1f}s"
    response = polled["response"]
    assert response.status_code == 200, f"poll failed: {response.status_code} {response.text}"
    body = response.json()
    assert [wake["reason"] for wake in body] == ["mention"], f"unexpected wakes: {body}"
    wake = body[0]
    assert wake["source_ref"] == f"post:{post_id}", f"wake points at the wrong source: {wake}"


async def test_poll_returns_immediately_when_wakes_pending(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        await _bus(app).emit(_mention(pi.principal, 7))
        started = time.monotonic()

        wakes = await _poll(client, headers, wait=30)

        elapsed = time.monotonic() - started
    assert elapsed < 5, f"poll waited despite a pending wake: {elapsed:.1f}s"
    assert [wake["source_ref"] for wake in wakes] == ["post:7"], f"pending wake not returned: {wakes}"


async def test_ack_removes_wake_from_next_poll(settings: Settings, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        for post_id in (1, 2):
            await _bus(app).emit(_mention(pi.principal, post_id))
        first, second = await _poll(client, headers)

        r = await client.post("/api/wakes/ack", json={"ids": [first["id"]]}, headers=headers)

        assert r.status_code == 200, f"ack refused: {r.status_code} {r.text}"
        _expire_leases(settings)
        remaining = await _poll(client, headers)
    assert [wake["id"] for wake in remaining] == [second["id"]], f"acked wake came back: {remaining}"


async def test_leased_wake_reappears_after_lease_expiry(settings: Settings, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        await _bus(app).emit(_mention(pi.principal, 7))
        leased = await _poll(client, headers)
        while_leased = await _poll(client, headers)

        _expire_leases(settings)
        after_expiry = await _poll(client, headers)

    assert len(leased) == 1, f"first poll should lease the wake: {leased}"
    assert while_leased == [], f"leased wake delivered twice within its lease: {while_leased}"
    reappeared = [wake["id"] for wake in after_expiry]
    assert reappeared == [leased[0]["id"]], f"wake did not reappear after lease expiry: {after_expiry}"


async def test_timer_tick_emits_wake_when_interval_elapsed(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        profile = {"display_name": "pi on testbox", "specialities": ["shell"], "wake_every": "PT1H"}
        r = await _send(client, pi, "introduce", "", profile)
        assert r.status_code == 200, f"introduce refused: {r.status_code} {r.text}"
        now = time.time()
        with closing(sqlite3.connect(settings.db_path)) as conn, conn:
            conn.execute(
                "UPDATE profiles SET last_timer_wake_at = ? WHERE principal = ?",
                (now - 2 * 3600, pi.principal),
            )

        for _ in range(2):
            await scheduler.tick(app.state.db, _bus(app), now)

        wakes = await _poll(client, headers)
    assert [wake["reason"] for wake in wakes] == ["timer"], f"expected one timer wake per interval: {wakes}"


async def test_human_principal_readable_by_any_runner(settings: Settings, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        await _bus(app).emit(_mention(HUMAN, 7))

        human_wakes = await _poll(client, headers, principal=HUMAN)

    owners = [wake["principal"] for wake in human_wakes]
    assert owners == [HUMAN], f"runner could not read the human's wakes: {human_wakes}"


async def test_human_wakes_acked_by_any_runner(settings: Settings, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        await _bus(app).emit(_mention(HUMAN, 7))
        (human_wake,) = await _poll(client, headers, principal=HUMAN)

        r = await client.post(
            "/api/wakes/ack", json={"ids": [human_wake["id"]], "principal": HUMAN}, headers=headers
        )

        assert r.status_code == 200, f"ack refused: {r.status_code} {r.text}"
        _expire_leases(settings)
        remaining = await _poll(client, headers, principal=HUMAN)
    assert remaining == [], f"acked human wake came back: {remaining}"


def _age_wakes(settings: Settings, principal: str, seconds: float) -> None:
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE wakes SET created_at = created_at - ? WHERE principal = ?", (seconds, principal))


async def test_human_wakes_wait_for_the_digest_window_then_lease_together(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(dataclasses.replace(settings, human_digest_seconds=600)) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        await _bus(app).emit(_mention(HUMAN, 7))
        await _bus(app).emit(_mention(pi.principal, 8))
        too_soon = await _poll(client, headers, principal=HUMAN)
        agent_wakes = await _poll(client, headers)

        _age_wakes(settings, HUMAN, 601)
        await _bus(app).emit(_mention(HUMAN, 9))
        await _bus(app).emit(_mention(HUMAN, 10))
        digest = await _poll(client, headers, principal=HUMAN)

    assert too_soon == [], f"a human wake was leased inside the digest window: {too_soon}"
    assert [wake["source_ref"] for wake in agent_wakes] == ["post:8"], f"agent wakes were held: {agent_wakes}"
    refs = [wake["source_ref"] for wake in digest]
    assert refs == ["post:7", "post:9", "post:10"], (
        f"the digest did not lease every pending human wake: {refs}"
    )


async def test_unacked_digest_is_handed_out_again_after_its_lease(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(dataclasses.replace(settings, human_digest_seconds=600)) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        await _bus(app).emit(_mention(HUMAN, 7))
        _age_wakes(settings, HUMAN, 601)
        first = await _poll(client, headers, principal=HUMAN)
        while_leased = await _poll(client, headers, principal=HUMAN)
        _expire_leases(settings)
        again = await _poll(client, headers, principal=HUMAN)

    assert [wake["source_ref"] for wake in first] == ["post:7"], first
    assert while_leased == [], f"a leased digest was handed out twice: {while_leased}"
    assert [wake["source_ref"] for wake in again] == ["post:7"], f"an unacked digest was lost: {again}"


async def test_a_matured_digest_larger_than_a_lease_batch_goes_to_one_relay(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    claude = signer("claude@otherbox")
    async with _running_board(dataclasses.replace(settings, human_digest_seconds=600)) as (app, client):
        _register(app, pi, claude)
        pi_headers = await _bearer(client, pi)
        claude_headers = await _bearer(client, claude)
        for post_id in range(LEASE_BATCH + 5):
            await _bus(app).emit(_mention(HUMAN, post_id))
        _age_wakes(settings, HUMAN, 601)
        async with anyio.create_task_group() as group:
            leased: list[list[dict[str, Any]]] = []

            async def relay(headers: dict[str, str]) -> None:
                leased.append(await _poll(client, headers, principal=HUMAN))

            group.start_soon(relay, pi_headers)
            group.start_soon(relay, claude_headers)

    sizes = sorted(len(batch) for batch in leased)
    assert sizes == [0, LEASE_BATCH + 5], f"the digest was split across relays: {sizes}"


async def test_poll_updates_last_poll_at(settings: Settings, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        before = time.time()

        await _poll(client, headers)

        after = time.time()
    with closing(sqlite3.connect(settings.db_path)) as conn:
        row = conn.execute(
            "SELECT last_poll_at FROM principals WHERE principal = ?", (pi.principal,)
        ).fetchone()
    assert row is not None and row[0] is not None, f"poll left no last_poll_at for {pi.principal}: {row}"
    assert before <= row[0] <= after, f"last_poll_at {row[0]} outside the poll [{before}, {after}]"


async def test_poll_abandoned_by_client_does_not_lease_next_wake(
    settings: Settings, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, pi)
        headers = await _bearer(client, pi)
        hung_up = anyio.Event()
        sent: list[MutableMapping[str, Any]] = []

        async def receive() -> MutableMapping[str, Any]:
            if not sent:
                sent.append({})
                return {"type": "http.request", "body": b"", "more_body": False}
            if not hung_up.is_set():
                await hung_up.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            sent.append(message)

        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/wakes",
            "raw_path": b"/api/wakes",
            "query_string": b"wait=30",
            "root_path": "",
            "headers": [(b"host", b"board"), (b"authorization", headers["Authorization"].encode())],
            "client": ("127.0.0.1", 1),
            "server": ("board", 80),
        }
        started = time.monotonic()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(app, scope, receive, send)
            await anyio.sleep(0.3)
            hung_up.set()
            await anyio.sleep(1.5)
            await _bus(app).emit(_mention(pi.principal, 7))
        elapsed = time.monotonic() - started

        wakes = await _poll(client, headers)
    assert elapsed < 10, f"abandoned poll kept waiting: {elapsed:.1f}s"
    assert [wake["source_ref"] for wake in wakes] == ["post:7"], f"wake leased to a dead poll: {wakes}"


async def test_timer_tick_failure_for_one_principal_does_not_skip_others(
    settings: Settings, signer: "SignerFactory", monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        _register(app, claude, pi)
        headers = await _bearer(client, pi)
        for each in (claude, pi):
            profile = {"display_name": each.principal, "specialities": ["shell"], "wake_every": "PT1H"}
            r = await _send(client, each, "introduce", "", profile)
            assert r.status_code == 200, f"introduce refused: {r.status_code} {r.text}"
        now = time.time()
        with closing(sqlite3.connect(settings.db_path)) as conn, conn:
            conn.execute("UPDATE profiles SET last_timer_wake_at = ?", (now - 2 * 3600,))
        bus = _bus(app)
        real_emit = bus.emit

        async def flaky_emit(request: WakeRequest) -> None:
            if request.principal == claude.principal:
                raise sqlite3.OperationalError("database is locked")
            await real_emit(request)

        monkeypatch.setattr(bus, "emit", flaky_emit)

        await scheduler.tick(app.state.db, bus, now)

        wakes = await _poll(client, headers)
    assert [wake["reason"] for wake in wakes] == ["timer"], (
        f"later principal skipped after a failure: {wakes}"
    )


def test_runner_poll_wait_cap_matches_board_cap() -> None:
    from agent_board import client as board_client
    from agent_board.runner.config import MAX_POLL_WAIT_SECONDS
    from board.wakes.routes import MAX_WAIT_SECONDS

    assert MAX_POLL_WAIT_SECONDS == MAX_WAIT_SECONDS, "runner and board disagree on the poll wait cap"
    read_timeout = board_client._TIMEOUT.read
    assert read_timeout is not None and read_timeout > MAX_WAIT_SECONDS, (
        f"client read timeout {read_timeout} does not outlast the board's {MAX_WAIT_SECONDS} s poll"
    )
