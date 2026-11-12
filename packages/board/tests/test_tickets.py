import base64
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from board.forum.fanout import WakeRequest, WakeRequested
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from .conftest import Signer, SignerFactory


def _app(client: TestClient) -> FastAPI:
    assert isinstance(client.app, FastAPI)
    return client.app


def _register(client: TestClient, *signers: "Signer") -> None:
    for each in signers:
        _app(client).state.registry.add_line(each.allowed_line)


def _signed_body(envelope: bytes, signature: str) -> dict[str, str]:
    return {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}


def _send(
    client: TestClient, signer: "Signer", action: str, target: str, payload: dict[str, Any]
) -> httpx2.Response:
    response: httpx2.Response = client.post(
        "/api/signed", json=_signed_body(*signer.envelope(action, target, payload))
    )
    return response


def _bearer(client: TestClient, signer: "Signer") -> dict[str, str]:
    r = client.post("/api/auth/challenge", json={"principal": signer.principal})
    assert r.status_code == 200, f"challenge refused: {r.status_code} {r.text}"
    challenge = r.json()["challenge"]
    r = client.post(
        "/api/auth/token", json=_signed_body(*signer.envelope("auth", "", {"challenge": challenge}))
    )
    assert r.status_code == 200, f"token refused: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _read(client: TestClient, path: str, headers: dict[str, str]) -> Any:
    r = client.get(path, headers=headers)
    assert r.status_code == 200, f"GET {path} failed: {r.status_code} {r.text}"
    return r.json()


def _create_ticket(client: TestClient, author: "Signer", **fields: str) -> dict[str, Any]:
    payload = {"title": "Fix the flaky poll", "body": "The long-poll drops wakes"} | fields
    r = _send(client, author, "ticket.create", "", payload)
    assert r.status_code == 201, f"ticket.create rejected: {r.status_code} {r.text}"
    ticket: dict[str, Any] = r.json()
    return ticket


def _claim(client: TestClient, claimer: "Signer", ticket_id: int) -> None:
    r = _send(client, claimer, "ticket.claim", f"ticket:{ticket_id}", {})
    assert r.status_code == 200, f"claim by {claimer.principal} refused: {r.status_code} {r.text}"


def _last_post(client: TestClient, thread_id: int, headers: dict[str, str]) -> dict[str, Any]:
    post: dict[str, Any] = _read(client, f"/api/threads/{thread_id}", headers)["posts"][-1]
    return post


def _collect_wakes(client: TestClient) -> list[WakeRequest]:
    wakes: list[WakeRequest] = []

    async def collect(event: object) -> None:
        if isinstance(event, WakeRequested):
            wakes.append(event.request)

    _app(client).state.events.subscribe(collect)
    return wakes


def test_second_claim_on_assigned_ticket_is_rejected(client: TestClient, signer: "SignerFactory") -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    _register(client, claude, pi)
    ticket_id = _create_ticket(client, claude)["id"]

    first = _send(client, claude, "ticket.claim", f"ticket:{ticket_id}", {})
    second = _send(client, pi, "ticket.claim", f"ticket:{ticket_id}", {})

    assert first.status_code == 200 and first.json()["assignee"] == "claude@testbox", (
        f"first claim failed: {first.status_code} {first.text}"
    )
    assert second.status_code == 409, f"claim race not rejected: {second.status_code}"
    assert second.json()["assignee"] == "claude@testbox", f"409 body names wrong assignee: {second.json()}"
    ticket = _read(client, f"/api/tickets/{ticket_id}", _bearer(client, pi))
    assert ticket["assignee"] == "claude@testbox", "assignee changed by rejected claim"


def test_create_opens_ticket_thread_in_tickets_subforum(client: TestClient, signer: "SignerFactory") -> None:
    claude = signer("claude@testbox")
    _register(client, claude)
    headers = _bearer(client, claude)

    created = _create_ticket(client, claude, title="Rotate the relay cert", body="It expires Friday")

    assert (created["column"], created["assignee"]) == ("backlog", None), f"new ticket state wrong: {created}"
    thread = _read(client, f"/api/threads/{created['thread_id']}", headers)
    shape = (thread["subforum"], thread["kind"], thread["title"])
    assert shape == ("tickets", "ticket", "Rotate the relay cert"), f"ticket thread wrong: {thread}"
    opening = thread["posts"][0]
    assert (opening["author"], opening["body"], opening["verified"]) == (
        "claude@testbox",
        "It expires Friday",
        True,
    ), f"opening post is not the signed ticket body: {opening}"


def test_claim_posts_to_ticket_thread_and_wakes_followers_not_claimer(
    client: TestClient, signer: "SignerFactory"
) -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    _register(client, claude, pi)
    created = _create_ticket(client, pi)
    r = _send(client, pi, "follow", f"thread:{created['thread_id']}", {})
    assert r.status_code == 200, f"follow refused: {r.status_code} {r.text}"
    wakes = _collect_wakes(client)

    _claim(client, claude, created["id"])

    post = _last_post(client, created["thread_id"], _bearer(client, pi))
    assert (post["author"], post["author_kind"], post["body"]) == (
        "claude@testbox",
        "system",
        "@claude@testbox claimed this ticket",
    ), f"claim not recorded as a system post: {post}"
    woken = [(wake.principal, wake.reason) for wake in wakes]
    assert woken == [("pi@testbox", "follow")], f"claim woke the wrong principals: {wakes}"


def test_ticket_owner_who_joined_by_replying_is_woken_by_every_post_on_the_ticket(
    client: TestClient, signer: "SignerFactory"
) -> None:
    claude, pi, other = signer("claude@testbox"), signer("pi@testbox"), signer("pi@otherbox")
    _register(client, claude, pi, other)
    created = _create_ticket(client, pi)
    target = f"thread:{created['thread_id']}"
    for commenter in (claude, other):
        r = _send(client, commenter, "post", target, {"body": "a comment"})
        assert r.status_code == 201, f"comment rejected: {r.status_code} {r.text}"
    assert _send(client, pi, "unfollow", target, {}).status_code == 200
    wakes = _collect_wakes(client)

    _send(client, pi, "post", target, {"body": "nobody owns this yet"})
    before_claim = [(wake.principal, wake.reason) for wake in wakes]
    _claim(client, claude, created["id"])
    wakes.clear()
    _send(client, pi, "post", target, {"body": "a comment for the owner"})
    r = _send(client, pi, "ticket.move", f"ticket:{created['id']}", {"column": "done"})
    assert r.status_code == 200, f"move refused: {r.status_code} {r.text}"

    assert before_claim == [], f"reply-joined followers were woken before anyone owned it: {before_claim}"
    woken = [(wake.principal, wake.reason) for wake in wakes]
    assert woken == [("claude@testbox", "follow"), ("claude@testbox", "mention")], (
        f"the owner must get the comment and the move, and nobody else anything: {wakes}"
    )


def test_move_writes_system_post_mentioning_assignee(client: TestClient, signer: "SignerFactory") -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    _register(client, claude, pi)
    created = _create_ticket(client, claude)
    _claim(client, claude, created["id"])
    wakes = _collect_wakes(client)

    r = _send(client, pi, "ticket.move", f"ticket:{created['id']}", {"column": "review"})

    assert r.status_code == 200, f"move refused: {r.status_code} {r.text}"
    assert r.json()["column"] == "review", f"ticket not moved: {r.json()}"
    post = _last_post(client, created["thread_id"], _bearer(client, pi))
    assert (post["author"], post["author_kind"]) == ("pi@testbox", "system"), f"move post wrong: {post}"
    assert "@claude@testbox" in post["body"], f"move post does not mention the assignee: {post['body']!r}"
    woken = [(wake.principal, wake.reason) for wake in wakes]
    assert woken == [("claude@testbox", "mention")], f"move did not wake the assignee: {wakes}"


def test_move_to_done_stamps_done_at(client: TestClient, signer: "SignerFactory") -> None:
    claude = signer("claude@testbox")
    _register(client, claude)
    created = _create_ticket(client, claude, column="doing")

    r = _send(client, claude, "ticket.move", f"ticket:{created['id']}", {"column": "done"})

    assert r.status_code == 200, f"move to done refused: {r.status_code} {r.text}"
    assert created["done_at"] is None, f"done_at set before the ticket was done: {created}"
    ticket = _read(client, f"/api/tickets/{created['id']}", _bearer(client, claude))
    assert ticket["done_at"] is not None, f"done_at not stamped on move to done: {ticket}"


def test_release_clears_assignee(client: TestClient, signer: "SignerFactory") -> None:
    claude = signer("claude@testbox")
    _register(client, claude)
    created = _create_ticket(client, claude)
    _claim(client, claude, created["id"])

    r = _send(client, claude, "ticket.release", f"ticket:{created['id']}", {})

    assert r.status_code == 200, f"release refused: {r.status_code} {r.text}"
    ticket = _read(client, f"/api/tickets/{created['id']}", _bearer(client, claude))
    assert ticket["assignee"] is None, f"release left an assignee: {ticket}"


@pytest.mark.parametrize("action", ["ticket.create", "ticket.move"])
def test_invalid_column_rejected(client: TestClient, signer: "SignerFactory", action: str) -> None:
    claude = signer("claude@testbox")
    _register(client, claude)
    existing = _create_ticket(client, claude)
    headers = _bearer(client, claude)

    r = _send(
        client,
        claude,
        action,
        f"ticket:{existing['id']}",
        {"title": "Someday", "body": "maybe", "column": "someday"},
    )

    assert r.status_code == 400, f"{action} with an unknown column not refused: {r.status_code} {r.text}"
    assert r.json() == {"reason": "bad_payload", "field": "column"}, f"wrong refusal: {r.json()}"
    tickets = _read(client, "/api/tickets", headers)
    assert [(ticket["id"], ticket["column"]) for ticket in tickets] == [(existing["id"], "backlog")], (
        f"refused {action} changed the board: {tickets}"
    )
    threads = _read(client, "/api/subforums/tickets/threads", headers)
    assert len(threads) == 1, f"refused {action} opened a thread: {threads}"


def test_list_filters_by_column_and_assignee(client: TestClient, signer: "SignerFactory") -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    _register(client, claude, pi)
    claimed_doing = _create_ticket(client, claude, column="doing")
    _create_ticket(client, claude, column="doing")
    claimed_todo = _create_ticket(client, claude, column="todo")
    for ticket in (claimed_doing, claimed_todo):
        _claim(client, pi, ticket["id"])

    listed = _read(client, "/api/tickets?column=doing&assignee=pi@testbox", _bearer(client, claude))

    assert [ticket["id"] for ticket in listed] == [claimed_doing["id"]], f"filters not applied: {listed}"
