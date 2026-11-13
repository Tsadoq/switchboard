import base64
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from html import unescape
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
    with TestClient(create_app(settings), base_url="https://testserver") as browser:
        yield browser


def _register(client: TestClient, *signers: "Signer") -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    for each in signers:
        app.state.registry.add_line(each.allowed_line)


def _send(
    client: TestClient, signer: "Signer", action: str, target: str, payload: dict[str, Any]
) -> httpx2.Response:
    envelope, signature = signer.envelope(action, target, payload)
    body = {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}
    response: httpx2.Response = client.post("/api/signed", json=body)
    return response


def _post(client: TestClient, author: "Signer", body: str = "Need a decision") -> tuple[int, int]:
    r = _send(client, author, "post", "subforum:general", {"title": "Rollout", "body": body})
    assert r.status_code == 201, f"post refused: {r.status_code} {r.text}"
    return r.json()["thread_id"], r.json()["post_id"]


def _open(
    client: TestClient, asker: "Signer", post_id: int, ask: str = "Approve the deploy"
) -> dict[str, Any]:
    r = _send(client, asker, "ask.open", f"post:{post_id}", {"ask": ask})
    assert r.status_code == 201, f"ask.open refused: {r.status_code} {r.text}"
    opened: dict[str, Any] = r.json()
    return opened


def _bearer(client: TestClient, signer: "Signer") -> dict[str, str]:
    r = client.post("/api/auth/challenge", json={"principal": signer.principal})
    challenge = r.json()["challenge"]
    envelope, signature = signer.envelope("auth", "", {"challenge": challenge})
    body = {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}
    r = client.post("/api/auth/token", json=body)
    assert r.status_code == 200, f"token refused: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _asks(client: TestClient, reader: "Signer", state: str = "open") -> list[dict[str, Any]]:
    r = client.get("/api/asks", params={"state": state}, headers=_bearer(client, reader))
    assert r.status_code == 200, f"listing refused: {r.status_code} {r.text}"
    asks: list[dict[str, Any]] = r.json()
    return asks


def _log_in(client: TestClient, password: str) -> None:
    r = client.post("/login", data={"password": password}, follow_redirects=False)
    assert r.status_code == 303, f"login refused: {r.status_code} {r.text}"


def test_an_open_ask_is_listed_with_its_post_and_thread(client: TestClient, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    _register(client, pi)
    thread_id, post_id = _post(client, pi)

    opened = _open(client, pi, post_id)

    listed = _asks(client, pi)
    assert [(ask["id"], ask["post_id"], ask["thread_id"], ask["thread_title"]) for ask in listed] == [
        (opened["id"], post_id, thread_id, "Rollout")
    ]
    assert (listed[0]["asker"], listed[0]["ask"], listed[0]["closed_at"]) == (
        "pi@testbox",
        "Approve the deploy",
        None,
    )


def test_open_asks_are_listed_oldest_first(client: TestClient, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    _register(client, pi)
    first = _open(client, pi, _post(client, pi)[1], "first")
    second = _open(client, pi, _post(client, pi)[1], "second")

    assert [ask["id"] for ask in _asks(client, pi)] == [first["id"], second["id"]]


def test_only_the_posts_author_can_open_an_ask_on_it(client: TestClient, signer: "SignerFactory") -> None:
    pi, claude = signer("pi@testbox"), signer("claude@testbox")
    _register(client, pi, claude)
    _, post_id = _post(client, pi)

    r = _send(client, claude, "ask.open", f"post:{post_id}", {"ask": "Approve it"})

    assert (r.status_code, r.json()["reason"]) == (403, "not_post_author")
    assert _asks(client, pi) == []


def test_a_post_has_at_most_one_open_ask(client: TestClient, signer: "SignerFactory") -> None:
    pi = signer("pi@testbox")
    _register(client, pi)
    _, post_id = _post(client, pi)
    first = _open(client, pi, post_id)

    again = _send(client, pi, "ask.open", f"post:{post_id}", {"ask": "Again"})
    assert (again.status_code, again.json()["reason"]) == (409, "ask_open")

    closed = _send(client, pi, "ask.close", f"ask:{first['id']}", {"outcome": "Withdrawn"})
    assert closed.status_code == 200, closed.text
    _open(client, pi, post_id, "Asking again after the first was withdrawn")


@pytest.mark.parametrize(
    ("target", "payload", "status", "reason"),
    [
        pytest.param("post:999", {"ask": "x"}, 404, "no_such_post", id="missing-post"),
        pytest.param("thread:1", {"ask": "x"}, 400, "bad_target", id="wrong-scope"),
        pytest.param("post:{post}", {}, 400, "bad_payload", id="no-ask"),
        pytest.param("post:{post}", {"ask": "   "}, 400, "bad_payload", id="blank"),
        pytest.param("post:{post}", {"ask": "two\nlines"}, 400, "bad_payload", id="two-lines"),
        pytest.param("post:{post}", {"ask": "x" * 201}, 400, "bad_payload", id="too-long"),
        pytest.param("post:{post}", {"ask": 7}, 400, "bad_payload", id="not-text"),
    ],
)
def test_a_malformed_open_is_refused(
    client: TestClient,
    signer: "SignerFactory",
    target: str,
    payload: dict[str, Any],
    status: int,
    reason: str,
) -> None:
    pi = signer("pi@testbox")
    _register(client, pi)
    _, post_id = _post(client, pi)

    r = _send(client, pi, "ask.open", target.format(post=post_id), payload)

    assert (r.status_code, r.json()["reason"]) == (status, reason)


def test_the_asker_closes_with_an_outcome_noted_on_the_thread(
    client: TestClient, signer: "SignerFactory"
) -> None:
    pi = signer("pi@testbox")
    _register(client, pi)
    thread_id, post_id = _post(client, pi)
    opened = _open(client, pi, post_id)

    r = _send(client, pi, "ask.close", f"ask:{opened['id']}", {"outcome": "Done on the box myself"})

    assert r.status_code == 200, r.text
    assert (r.json()["closed_by"], r.json()["outcome"]) == ("pi@testbox", "Done on the box myself")
    assert _asks(client, pi) == []
    assert [ask["id"] for ask in _asks(client, pi, "closed")] == [opened["id"]]
    thread = client.get(f"/api/threads/{thread_id}", headers=_bearer(client, pi)).json()
    assert (
        thread["posts"][-1]["body"]
        == f"pi@testbox closed ask {opened['id']} (post {post_id}): Done on the box myself"
    )


def test_another_agent_cannot_close_an_ask_and_a_closed_ask_stays_closed(
    client: TestClient, signer: "SignerFactory"
) -> None:
    pi, claude = signer("pi@testbox"), signer("claude@testbox")
    _register(client, pi, claude)
    opened = _open(client, pi, _post(client, pi)[1])

    stranger = _send(client, claude, "ask.close", f"ask:{opened['id']}", {"outcome": "Not mine"})
    assert (stranger.status_code, stranger.json()["reason"]) == (403, "not_asker")

    _send(client, pi, "ask.close", f"ask:{opened['id']}", {"outcome": "Resolved"})
    twice = _send(client, pi, "ask.close", f"ask:{opened['id']}", {"outcome": "Again"})
    assert (twice.status_code, twice.json()["reason"]) == (409, "ask_closed")
    missing = _send(client, pi, "ask.close", "ask:999", {"outcome": "x"})
    assert (missing.status_code, missing.json()["reason"]) == (404, "no_such_ask")


def test_the_human_sees_open_asks_and_closes_one_from_the_page(
    browser: TestClient, signer: "SignerFactory", human_password: str, settings: Settings
) -> None:
    pi = signer("pi@testbox")
    _register(browser, pi)
    thread_id, post_id = _post(browser, pi)
    opened = _open(browser, pi, post_id, "Approve <the> deploy")
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE human_asks SET created_at = created_at - 7500")

    assert browser.get("/asks", follow_redirects=False).headers["location"] == "/login"
    _log_in(browser, human_password)
    page = browser.get("/asks").text
    assert "Approve &lt;the&gt; deploy" in page and "waiting 2h 5m" in page
    assert f'href="/p/{post_id}"' in page

    r = browser.post(f"/asks/{opened['id']}/close", data={"outcome": "Approved"}, follow_redirects=False)

    assert (r.status_code, r.headers["location"]) == (303, "/asks")
    closed = _asks(browser, pi, "closed")[0]
    assert (closed["closed_by"], closed["outcome"]) == ("human", "Approved")
    thread = browser.get(f"/api/threads/{thread_id}").json()
    assert (thread["posts"][-1]["author"], thread["posts"][-1]["author_kind"]) == ("human", "human")
    assert "Nothing is waiting on you." in unescape(browser.get("/asks").text)


def test_a_blank_outcome_from_the_page_is_refused_with_a_reason(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    pi = signer("pi@testbox")
    _register(browser, pi)
    opened = _open(browser, pi, _post(browser, pi)[1])
    _log_in(browser, human_password)

    r = browser.post(f"/asks/{opened['id']}/close", data={"outcome": " "}, follow_redirects=False)

    assert r.headers["location"] == "/asks?error=bad_payload"
    assert "one line of at most 200 characters" in browser.get(r.headers["location"]).text
    assert [ask["id"] for ask in _asks(browser, pi)] == [opened["id"]]
