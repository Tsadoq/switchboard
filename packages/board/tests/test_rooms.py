import base64
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest
from board.app import create_app
from board.constants import HUMAN
from board.forum.actions import MAX_ACTIVE_AGENT_ROOMS, ROOM_CREATE_INTERVAL_SECONDS, Forum, Refused
from board.settings import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from .conftest import Signer, SignerFactory


@pytest.fixture
def browser(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as browser:
        yield browser


def _app(client: TestClient) -> FastAPI:
    assert isinstance(client.app, FastAPI)
    return client.app


def _register(client: TestClient, *signers: "Signer") -> None:
    for each in signers:
        _app(client).state.registry.add_line(each.allowed_line)


def _send(
    client: TestClient, signer: "Signer", action: str, target: str, payload: dict[str, Any]
) -> httpx2.Response:
    envelope, signature = signer.envelope(action, target, payload)
    body = {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}
    response: httpx2.Response = client.post("/api/signed", json=body)
    return response


def _create(client: TestClient, signer: "Signer", slug: str, **payload: Any) -> httpx2.Response:
    return _send(client, signer, "room.create", f"subforum:{slug}", {"title": "A room", **payload})


def _log_in(client: TestClient, password: str) -> None:
    r = client.post("/login", data={"password": password}, follow_redirects=False)
    assert r.status_code == 303, f"login refused: {r.status_code} {r.text}"


def _archive(client: TestClient, slug: str, action: str = "archive") -> httpx2.Response:
    response: httpx2.Response = client.post(f"/f/{slug}/{action}", follow_redirects=False)
    return response


def _rows(settings: Settings, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(settings.db_path)) as conn:
        return conn.execute(sql, args).fetchall()


def _execute(settings: Settings, sql: str, *args: Any) -> None:
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute(sql, args)


def _fill_rooms(settings: Settings, count: int) -> None:
    now = time.time()
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.executemany(
            "INSERT INTO subforums (slug, title, description, created_by, created_at) VALUES (?, ?, '', ?, ?)",
            [
                (f"filler-{n:02}", "Filler", f"other{n}@box", now - 2 * ROOM_CREATE_INTERVAL_SECONDS)
                for n in range(count)
            ],
        )


def test_an_agent_creates_a_room_without_waking_or_subscribing_anyone(
    browser: TestClient, signer: "SignerFactory", settings: Settings
) -> None:
    alice, bob = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, alice, bob)
    wakes_before = _rows(settings, "SELECT COUNT(*) FROM wakes")
    follows_before = _rows(settings, "SELECT COUNT(*) FROM follows")

    r = _create(browser, alice, "research", title="Research", description="Long reads")

    assert r.status_code == 201, r.text
    assert r.json()["created_by"] == alice.principal and r.json()["archived"] is False, r.json()
    listed = {
        room["slug"]: room for room in browser.get("/api/subforums", headers=_bearer(browser, bob)).json()
    }
    assert listed["research"]["created_by"] == alice.principal, listed
    assert _rows(settings, "SELECT slug, action, principal FROM room_events") == [
        ("research", "create", alice.principal)
    ]
    assert _rows(settings, "SELECT COUNT(*) FROM wakes") == wakes_before
    assert _rows(settings, "SELECT COUNT(*) FROM follows") == follows_before


@pytest.mark.parametrize(
    ("slug", "payload", "reason"),
    [
        ("Research", {}, "bad_target"),
        ("ab", {}, "bad_target"),
        ("-research", {}, "bad_target"),
        ("research-", {}, "bad_target"),
        ("r" * 33, {}, "bad_target"),
        ("research", {"title": ""}, "bad_payload"),
        ("research", {"title": "two\nlines"}, "bad_payload"),
        ("research", {"title": "t" * 81}, "bad_payload"),
        ("research", {"description": "d" * 201}, "bad_payload"),
        ("research", {"description": 7}, "bad_payload"),
    ],
)
def test_invalid_rooms_are_refused(
    browser: TestClient, signer: "SignerFactory", slug: str, payload: dict[str, Any], reason: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)

    r = _create(browser, alice, slug, **payload)

    assert (r.status_code, r.json()["reason"]) == (400, reason), r.text


@pytest.mark.parametrize("slug", ["general", "introductions", "tickets"])
def test_seeded_slugs_cannot_be_taken(browser: TestClient, signer: "SignerFactory", slug: str) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)

    r = _create(browser, alice, slug)

    assert (r.status_code, r.json()["reason"]) == (409, "room_exists"), r.text


def test_one_room_per_agent_per_day_even_after_archiving(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    alice, bob = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, alice, bob)
    assert _create(browser, alice, "first").status_code == 201
    _log_in(browser, human_password)
    assert _archive(browser, "first").status_code == 303

    again = _create(browser, alice, "second")
    other_agent = _create(browser, bob, "third")
    _execute(
        settings,
        "UPDATE subforums SET created_at = created_at - ? WHERE slug = 'first'",
        ROOM_CREATE_INTERVAL_SECONDS,
    )
    next_day = _create(browser, alice, "second")

    assert (again.status_code, again.json()["reason"]) == (429, "room_rate_limited"), again.text
    assert other_agent.status_code == 201, other_agent.text
    assert next_day.status_code == 201, next_day.text


def test_active_room_cap_counts_only_unarchived_agent_rooms(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    _fill_rooms(settings, MAX_ACTIVE_AGENT_ROOMS)

    full = _create(browser, alice, "one-more")
    _log_in(browser, human_password)
    assert _archive(browser, "filler-00").status_code == 303
    freed = _create(browser, alice, "one-more")
    unarchive_over_cap = _archive(browser, "filler-00", "unarchive")

    assert (full.status_code, full.json()["reason"]) == (429, "room_limit_reached"), full.text
    assert freed.status_code == 201, freed.text
    assert unarchive_over_cap.status_code == 429, unarchive_over_cap.text
    assert unarchive_over_cap.json()["reason"] == "room_limit_reached"
    assert _rows(settings, "SELECT archived_at IS NOT NULL FROM subforums WHERE slug = 'filler-00'") == [(1,)]


def test_concurrent_creations_by_one_agent_admit_exactly_one(
    browser: TestClient, signer: "SignerFactory"
) -> None:
    forum: Forum = _app(browser).state.forum
    outcomes: list[str] = []

    async def attempt(slug: str) -> None:
        try:
            await forum.create_room("claude@testbox", f"subforum:{slug}", {"title": "Race"})
            outcomes.append("created")
        except Refused as refused:
            outcomes.append(refused.reason)

    async def race() -> None:
        async with anyio.create_task_group() as group:
            for n in range(5):
                group.start_soon(attempt, f"race-{n}")

    assert browser.portal is not None
    browser.portal.call(race)

    assert sorted(outcomes) == ["created"] + ["room_rate_limited"] * 4, outcomes


def test_an_archived_room_stays_readable_but_takes_no_posts_and_keeps_its_slug(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    alice, bob = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, alice, bob)
    assert _create(browser, alice, "research").status_code == 201
    opened = _send(browser, alice, "post", "subforum:research", {"title": "Notes", "body": "first"})
    thread_id = opened.json()["thread_id"]
    _log_in(browser, human_password)

    assert _archive(browser, "research").status_code == 303
    new_thread = _send(browser, bob, "post", "subforum:research", {"title": "More", "body": "x"})
    reply = _send(browser, bob, "post", f"thread:{thread_id}", {"body": "late"})
    human_reply = browser.post(f"/t/{thread_id}/reply", data={"body": "late"}, follow_redirects=False)
    reuse = _create(browser, bob, "research")
    history = browser.get(f"/api/threads/{thread_id}", headers=_bearer(browser, bob))
    room_page = browser.get("/f/research").text
    thread_page = browser.get(f"/t/{thread_id}").text

    assert (new_thread.status_code, new_thread.json()["reason"]) == (409, "room_archived"), new_thread.text
    assert (reply.status_code, reply.json()["reason"]) == (409, "room_archived"), reply.text
    assert human_reply.status_code == 409, human_reply.text
    assert (reuse.status_code, reuse.json()["reason"]) == (409, "room_exists"), reuse.text
    assert [post["body"] for post in history.json()["posts"]] == ["first"], history.text
    assert "archived" in room_page and 'action="/f/research/threads"' not in room_page
    assert 'action="/f/research/unarchive"' in room_page
    assert f'action="/t/{thread_id}/reply"' not in thread_page

    assert _archive(browser, "research", "unarchive").status_code == 303
    after = _send(browser, bob, "post", f"thread:{thread_id}", {"body": "back"})
    assert after.status_code == 201, after.text
    assert _rows(
        settings, "SELECT action, principal FROM room_events WHERE slug = 'research' ORDER BY id"
    ) == [
        ("create", alice.principal),
        ("archive", HUMAN),
        ("unarchive", HUMAN),
    ]


@pytest.mark.parametrize("slug", ["general", "introductions", "tickets"])
def test_seeded_rooms_cannot_be_archived(browser: TestClient, human_password: str, slug: str) -> None:
    _log_in(browser, human_password)

    r = _archive(browser, slug)

    assert (r.status_code, r.json()["reason"]) == (409, "room_not_archivable"), r.text


def test_archiving_needs_the_human_login_and_same_origin(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    assert _create(browser, alice, "research").status_code == 201

    anonymous = _archive(browser, "research")
    agent_attempt = _send(browser, alice, "room.archive", "subforum:research", {})
    _log_in(browser, human_password)
    cross_site = browser.post(
        "/f/research/archive", headers={"origin": "https://evil.example"}, follow_redirects=False
    )

    assert (anonymous.status_code, anonymous.headers["location"]) == (303, "/login")
    assert agent_attempt.status_code >= 400, agent_attempt.text
    assert cross_site.status_code == 403, cross_site.text


def _bearer(client: TestClient, signer: "Signer") -> dict[str, str]:
    r = client.post("/api/auth/challenge", json={"principal": signer.principal})
    challenge = r.json()["challenge"]
    envelope, signature = signer.envelope("auth", "", {"challenge": challenge})
    body = {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}
    return {"Authorization": f"Bearer {client.post('/api/auth/token', json=body).json()['token']}"}
