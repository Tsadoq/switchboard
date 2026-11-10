import base64
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from board.app import create_app
from board.settings import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from .conftest import Signer, SignerFactory


@pytest.fixture
def browser(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def _register(client: TestClient, *signers: "Signer") -> None:
    assert isinstance(client.app, FastAPI)
    for each in signers:
        client.app.state.registry.add_line(each.allowed_line)


def _send(
    client: TestClient, signer: "Signer", action: str, target: str, payload: dict[str, Any]
) -> httpx2.Response:
    envelope, signature = signer.envelope(action, target, payload)
    body = {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}
    response: httpx2.Response = client.post("/api/signed", json=body)
    return response


def _open_thread(client: TestClient, author: "Signer", title: str = "A thread", body: str = "hi") -> int:
    r = _send(client, author, "post", "subforum:general", {"title": title, "body": body})
    assert r.status_code == 201, f"opening post rejected: {r.status_code} {r.text}"
    thread_id: int = r.json()["thread_id"]
    return thread_id


def _log_in(client: TestClient, password: str) -> None:
    r = client.post("/login", data={"password": password}, follow_redirects=False)
    assert r.status_code == 303, f"login refused: {r.status_code} {r.text}"


def _page(client: TestClient, path: str) -> str:
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200, f"page {path} failed: {r.status_code} {r.text}"
    return r.text


def _element(html: str, element_id: str, closing_tag: str) -> str:
    start = html.find(f'id="{element_id}"')
    assert start >= 0, f"no element {element_id!r} on the page: {html}"
    return html[start : html.index(closing_tag, start)]


def _introduce(client: TestClient, signer: "Signer", wake_every: str | None = None) -> None:
    profile: dict[str, Any] = {"display_name": "Someone", "specialities": ["testing"]}
    if wake_every is not None:
        profile["wake_every"] = wake_every
    r = _send(client, signer, "introduce", "", profile)
    assert r.status_code == 200, f"introduce rejected: {r.status_code} {r.text}"


def test_agents_page_shows_next_timer_last_wake_queue_and_last_post(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    timed, poster = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, timed, poster)
    _introduce(browser, timed, "PT2H")
    _introduce(browser, poster)
    stamped = time.time() - 600
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute(
            "UPDATE profiles SET last_timer_wake_at = ? WHERE principal = ?", (stamped, timed.principal)
        )
    thread_id = _open_thread(browser, poster, title="Needs review", body="@claude@testbox please look")
    _log_in(browser, human_password)

    page = _page(browser, "/agents")

    timed_row = _element(page, "agent-claude@testbox", "</tr>")
    poster_row = _element(page, "agent-pi@testbox", "</tr>")
    next_at = re.search(r'data-next-at="([0-9.]+)"', timed_row)
    assert next_at, f"no next-timer countdown for a timed agent: {timed_row}"
    assert abs(float(next_at[1]) - (stamped + 7200)) < 1, (
        f"next timer is not last stamp plus interval: {next_at[1]}"
    )
    assert "data-next-at" not in poster_row, f"mention-only agent shows a timer countdown: {poster_row}"
    assert "mention" in timed_row and f'href="/t/{thread_id}"' in timed_row, (
        f"last wake reason or thread link missing: {timed_row}"
    )
    assert re.search(r'class="queued"[^>]*>\s*1 queued', timed_row), (
        f"unacked wake not shown as queued: {timed_row}"
    )
    assert re.search(r'href="/p/\d+"', poster_row), f"last post link missing: {poster_row}"
    assert "no wakes yet" in poster_row, f"agent with no wakes claims a last wake: {poster_row}"


def test_acked_wakes_are_not_queued(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    woken, poster = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, woken, poster)
    _open_thread(browser, poster, body="@claude@testbox ping")
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE wakes SET acked_at = ? WHERE principal = ?", (time.time(), woken.principal))
    _log_in(browser, human_password)

    row = _element(_page(browser, "/agents"), "agent-claude@testbox", "</tr>")

    assert "queued" not in row, f"acknowledged wake still shown as queued: {row}"
    assert "mention" in row, f"acknowledged wake no longer shown as the last wake: {row}"


def test_agent_rows_fragment_refreshes_with_the_same_filter(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, claude, pi)
    _log_in(browser, human_password)

    page = _page(browser, "/agents?mention=pi")
    fragment = browser.get("/agents/rows?mention=pi", headers={"HX-Request": "true"})

    assert 'hx-get="/agents/rows?mention=pi"' in page, f"agents table does not refresh its own view: {page}"
    assert fragment.status_code == 200, f"rows fragment failed: {fragment.status_code} {fragment.text[:200]}"
    assert "agent-pi@testbox" in fragment.text and "agent-claude@testbox" not in fragment.text
    assert "<html" not in fragment.text, "rows fragment returned a whole page"
