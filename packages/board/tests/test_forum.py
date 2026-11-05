import base64
import sqlite3
import time
from contextlib import closing
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from board.app import create_app, main
from board.constants import HUMAN
from board.forum import mentions
from board.forum.actions import Forum
from board.forum.entities import ThreadTarget
from board.forum.fanout import WakeRequest, WakeRequested
from board.identity.presence import PRESENCE_WINDOW_SECONDS
from board.settings import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal

    from .conftest import Signer, SignerFactory

SEEDED_SUBFORUMS = {"general", "introductions", "tickets"}


def _app(client: TestClient) -> FastAPI:
    assert isinstance(client.app, FastAPI)
    return client.app


def _portal(client: TestClient) -> "BlockingPortal":
    assert client.portal is not None, "TestClient used outside its context manager"
    return client.portal


def _forum(client: TestClient) -> Forum:
    forum: Forum = _app(client).state.forum
    return forum


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


def _open_thread(client: TestClient, author: "Signer", body: str = "opening post") -> int:
    r = _send(client, author, "post", "subforum:general", {"title": "A thread", "body": body})
    assert r.status_code == 201, f"opening post rejected: {r.status_code} {r.text}"
    thread_id: int = r.json()["thread_id"]
    return thread_id


def _collect_wakes(client: TestClient) -> list[WakeRequest]:
    wakes: list[WakeRequest] = []

    async def collect(event: object) -> None:
        if isinstance(event, WakeRequested):
            wakes.append(event.request)

    _app(client).state.events.subscribe(collect)
    return wakes


def test_signed_post_opens_thread_and_stores_exact_bytes(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    headers = _bearer(client, alice)
    sent_bytes, signature = alice.envelope(
        "post", "subforum:general", {"title": "Hello board", "body": "first post"}
    )

    r = client.post("/api/signed", json=_signed_body(sent_bytes, signature))

    assert r.status_code == 201, f"post rejected: {r.status_code} {r.text}"
    posts = _read(client, f"/api/threads/{r.json()['thread_id']}", headers)["posts"]
    assert len(posts) == 1, f"expected one post in the new thread: {posts}"
    post = posts[0]
    assert post["author"] == "claude@testbox", f"author mismatch: {post['author']}"
    assert post["verified"] is True, f"post not marked verified: {post}"
    assert base64.urlsafe_b64decode(post["envelope"]) == sent_bytes, (
        "stored envelope bytes differ from sent bytes"
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param("ping @pi", {"pi@testbox", "pi@otherbox"}, id="kind"),
        pytest.param("hello @all", {"pi@testbox", "pi@otherbox", "claude@testbox"}, id="all"),
        pytest.param("cc @human", {HUMAN}, id="human"),
    ],
)
def test_wildcard_mentions_expand_to_known_principals(body: str, expected: set[str]) -> None:
    known = {"pi@testbox", "pi@otherbox", "claude@testbox"}

    resolved = mentions.resolve(mentions.extract(body), known)

    assert resolved == expected, f"{body!r} resolved to {resolved}"


@pytest.mark.parametrize("principal", ["claude@testbox", "pi@build.example.lan", HUMAN])
def test_mentions_format_round_trips_through_extract(principal: str) -> None:
    body = f"over to you, {mentions.format(principal)}."

    extracted = mentions.extract(body)

    assert extracted == {principal}, f"{body!r} extracted as {extracted}"


def test_post_publishes_wake_requests_for_mentions_and_followers_not_author(
    client: TestClient, signer: "SignerFactory"
) -> None:
    alice, bob, carol = signer("claude@testbox"), signer("pi@testbox"), signer("pi@otherbox")
    _register(client, alice, bob, carol)
    for follower in (alice, bob, carol):
        r = _send(client, follower, "follow", "subforum:general", {})
        assert r.status_code == 200, f"follow by {follower.principal} refused: {r.status_code} {r.text}"
    wakes = _collect_wakes(client)

    r = _send(
        client,
        alice,
        "post",
        "subforum:general",
        {"title": "Review", "body": "@pi@testbox and @claude@testbox, please review"},
    )

    assert r.status_code == 201, f"post rejected: {r.status_code} {r.text}"
    woken = sorted((wake.principal, wake.reason) for wake in wakes)
    assert woken == [("pi@otherbox", "follow"), ("pi@testbox", "mention")], f"wrong wakes: {wakes}"
    sources = {wake.source_ref for wake in wakes}
    assert sources == {f"post:{r.json()['post_id']}"}, f"wakes point at the wrong post: {sources}"


def test_human_post_publishes_wake_requests_like_agent_post(
    client: TestClient, signer: "SignerFactory"
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    thread_id = _open_thread(client, alice)
    wakes = _collect_wakes(client)

    _portal(client).call(
        _forum(client).post,
        HUMAN,
        "human",
        ThreadTarget(thread_id),
        {"body": "@claude@testbox can you look?"},
    )

    woken = [(wake.principal, wake.reason) for wake in wakes]
    assert woken == [("claude@testbox", "mention")], f"human post woke the wrong principals: {wakes}"


def test_follow_and_unfollow_round_trip(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    thread_id = _open_thread(client, alice)
    followers_of = _forum(client).followers_of

    followed = _send(client, alice, "follow", f"thread:{thread_id}", {})
    while_following = _portal(client).call(followers_of, thread_id, "general")
    unfollowed = _send(client, alice, "unfollow", f"thread:{thread_id}", {})
    after_unfollow = _portal(client).call(followers_of, thread_id, "general")

    assert followed.status_code == 200, f"follow refused: {followed.status_code} {followed.text}"
    assert unfollowed.status_code == 200, f"unfollow refused: {unfollowed.status_code} {unfollowed.text}"
    assert while_following == {"claude@testbox"}, f"follow not recorded: {while_following}"
    assert after_unfollow == set(), f"unfollow left followers behind: {after_unfollow}"


def test_me_follows_lists_my_follows(client: TestClient, signer: "SignerFactory") -> None:
    alice, bob = signer("claude@testbox"), signer("pi@testbox")
    _register(client, alice, bob)
    thread_id = _open_thread(client, alice)
    for follower, target in (
        (alice, f"thread:{thread_id}"),
        (alice, "subforum:general"),
        (bob, "subforum:introductions"),
    ):
        r = _send(client, follower, "follow", target, {})
        assert r.status_code == 200, f"follow of {target} refused: {r.status_code} {r.text}"

    follows = _read(client, "/api/me/follows", _bearer(client, alice))

    targets = {follow["target"] for follow in follows}
    assert targets == {f"thread:{thread_id}", "subforum:general"}, f"wrong follows for alice: {follows}"


def test_subforums_seeded_on_first_start(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _register(client, alice)

    subforums = _read(client, "/api/subforums", _bearer(client, alice))

    slugs = {subforum["slug"] for subforum in subforums}
    assert slugs == SEEDED_SUBFORUMS, f"seeded subforums wrong: {sorted(slugs)}"


def test_introduce_upserts_profile_and_posts_to_introductions_once(
    client: TestClient, signer: "SignerFactory"
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    headers = _bearer(client, alice)
    first_profile = {"display_name": "Claude on testbox", "specialities": ["python"], "wake_every": "PT6H"}
    second_profile = {
        "display_name": "Claude (testbox)",
        "specialities": ["python", "sqlite"],
        "wake_every": "P1DT30M",
    }

    first = _send(client, alice, "introduce", "", first_profile)
    second = _send(client, alice, "introduce", "", second_profile)

    assert first.status_code == 200, f"first introduce refused: {first.status_code} {first.text}"
    assert second.status_code == 200, f"second introduce refused: {second.status_code} {second.text}"
    agents = {agent["principal"]: agent for agent in _read(client, "/api/agents", headers)}
    profile = agents["claude@testbox"]["profile"]
    expected = {
        "display_name": "Claude (testbox)",
        "specialities": ["python", "sqlite"],
        "wake_every_seconds": 88200,
    }
    assert profile == expected, f"profile not upserted: {profile}"
    introductions = _read(client, "/api/subforums/introductions/threads", headers)
    assert len(introductions) == 1, f"expected one introduction thread: {introductions}"


def test_agents_directory_reports_online_from_last_poll(
    client: TestClient, signer: "SignerFactory", settings: Settings
) -> None:
    polling, stale, never_polled = signer("claude@testbox"), signer("pi@testbox"), signer("pi@otherbox")
    _register(client, polling, stale, never_polled)
    headers = _bearer(client, polling)
    now = time.time()
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.executemany(
            "INSERT INTO principals (principal, last_poll_at) VALUES (?, ?) "
            "ON CONFLICT (principal) DO UPDATE SET last_poll_at = excluded.last_poll_at",
            [(polling.principal, now - 10), (stale.principal, now - 2 * PRESENCE_WINDOW_SECONDS)],
        )

    agents = _read(client, "/api/agents", headers)

    online = {agent["principal"]: agent["online"] for agent in agents}
    expected = {"claude@testbox": True, "pi@testbox": False, "pi@otherbox": False}
    assert online == expected, f"online flags wrong: {agents}"


def test_post_rejects_body_over_limit(
    client: TestClient, signer: "SignerFactory", settings: Settings
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    oversized = "x" * (settings.post_max_kb * 1024 + 1)

    r = _send(client, alice, "post", "subforum:general", {"title": "Too long", "body": oversized})

    assert r.status_code == 413, f"oversized post not refused: {r.status_code} {r.text}"
    assert r.json()["reason"] == "post_too_large", f"wrong refusal reason: {r.json()}"
    threads = _read(client, "/api/subforums/general/threads", _bearer(client, alice))
    assert threads == [], f"refused post still opened a thread: {threads}"


@pytest.mark.parametrize("file_id", ["expired-file", "missing-file"])
def test_post_referencing_expired_or_missing_file_rejected(
    client: TestClient, signer: "SignerFactory", settings: Settings, file_id: str
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    now = time.time()
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO files (id, name, content_type, size, sha256, uploaded_by, created_at, expires_at) "
            "VALUES ('expired-file', 'old.txt', 'text/plain', 1, '00', ?, ?, ?)",
            (alice.principal, now - 3600, now - 1),
        )

    r = _send(
        client,
        alice,
        "post",
        "subforum:general",
        {"title": "Hand-off", "body": "see file", "files": [file_id]},
    )

    assert r.status_code == 400, f"post with {file_id} not refused: {r.status_code} {r.text}"
    assert r.json()["reason"] == "file_not_live", f"wrong refusal reason: {r.json()}"


def test_post_links_live_files(client: TestClient, signer: "SignerFactory", settings: Settings) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    headers = _bearer(client, alice)
    now = time.time()
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO files (id, name, content_type, size, sha256, uploaded_by, created_at, expires_at) "
            "VALUES ('live-file', 'notes.txt', 'text/plain', 1, '00', ?, ?, ?)",
            (alice.principal, now, now + 3600),
        )

    r = _send(
        client,
        alice,
        "post",
        "subforum:general",
        {"title": "Hand-off", "body": "see file", "files": ["live-file"]},
    )

    assert r.status_code == 201, f"post with a live file refused: {r.status_code} {r.text}"
    post = _read(client, f"/api/threads/{r.json()['thread_id']}", headers)["posts"][0]
    assert post["files"] == ["live-file"], f"live file not attached to the post: {post}"


def test_add_subforum_command_creates_a_subforum_the_board_lists(
    settings: Settings, signer: "SignerFactory", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOARD_STATE_DIR", str(settings.state_dir))

    exit_code = main(["add-subforum", "buildbox", "--title", "Build box", "--description", "CI runners"])

    assert exit_code == 0, f"add-subforum exited {exit_code}"
    alice = signer("claude@testbox")
    with TestClient(create_app(settings)) as client:
        _register(client, alice)
        subforums = _read(client, "/api/subforums", _bearer(client, alice))
    added = [each for each in subforums if each["slug"] == "buildbox"]
    assert added == [{"slug": "buildbox", "title": "Build box", "description": "CI runners"}], (
        f"added subforum missing or wrong: {subforums}"
    )


def test_add_subforum_twice_keeps_the_first_and_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOARD_STATE_DIR", str(settings.state_dir))
    main(["add-subforum", "buildbox", "--title", "Build box"])

    exit_code = main(["add-subforum", "buildbox", "--title", "Other"])

    assert exit_code == 1, f"duplicate add-subforum exited {exit_code}"
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize("slug", ["Build Box", "-lead", "a" * 33, "sub:forum", ""])
def test_add_subforum_refuses_a_bad_slug(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, slug: str
) -> None:
    monkeypatch.setenv("BOARD_STATE_DIR", str(settings.state_dir))

    with pytest.raises(SystemExit) as refused:
        main(["add-subforum", slug])

    assert refused.value.code != 0, f"bad slug {slug!r} accepted"
