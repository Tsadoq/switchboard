import base64
import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from board.app import create_app
from board.identity.presence import PRESENCE_WINDOW_SECONDS
from board.settings import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from .conftest import Signer, SignerFactory

SESSION_COOKIE = "board_session"
SESSION_ENTROPY_BYTES = 32
THIRTY_DAYS_SECONDS = 30 * 86400
PAGES = ["/", "/f/general", "/t/1", "/tickets", "/files", "/agents"]


@pytest.fixture
def browser(settings: Settings) -> Iterator[TestClient]:
    # The default BOARD_PUBLIC_URL is https, so the session cookie is Secure and the cookie jar
    # only sends it back to an https origin.
    with TestClient(create_app(settings), base_url="https://testserver") as browser:
        yield browser


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


def _open_thread(
    client: TestClient, author: "Signer", subforum: str = "general", title: str = "A thread", body: str = "hi"
) -> int:
    r = _send(client, author, "post", f"subforum:{subforum}", {"title": title, "body": body})
    assert r.status_code == 201, f"opening post rejected: {r.status_code} {r.text}"
    thread_id: int = r.json()["thread_id"]
    return thread_id


def _log_in(client: TestClient, password: str) -> None:
    r = client.post("/login", data={"password": password}, follow_redirects=False)
    assert r.status_code == 303, f"login refused: {r.status_code} {r.text}"


def _submit(client: TestClient, path: str, **fields: str) -> httpx2.Response:
    response: httpx2.Response = client.post(path, data=fields, follow_redirects=False)
    assert response.status_code == 303, f"POST {path} refused: {response.status_code} {response.text}"
    return response


def _read(client: TestClient, path: str) -> Any:
    r = client.get(path)
    assert r.status_code == 200, f"GET {path} failed: {r.status_code} {r.text}"
    return r.json()


def _read_with_bearer(client: TestClient, signer: "Signer", path: str) -> Any:
    r = client.post("/api/auth/challenge", json={"principal": signer.principal})
    assert r.status_code == 200, f"challenge refused: {r.status_code} {r.text}"
    challenge = r.json()["challenge"]
    r = client.post(
        "/api/auth/token", json=_signed_body(*signer.envelope("auth", "", {"challenge": challenge}))
    )
    assert r.status_code == 200, f"token refused: {r.status_code} {r.text}"
    r = client.get(path, headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert r.status_code == 200, f"GET {path} failed: {r.status_code} {r.text}"
    return r.json()


def _page(client: TestClient, path: str) -> str:
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200, f"page {path} failed: {r.status_code} {r.text}"
    html: str = r.text
    return html


def _element(html: str, element_id: str, closing_tag: str) -> str:
    start = html.find(f'id="{element_id}"')
    assert start >= 0, f"no element {element_id!r} on the page: {html}"
    return html[start : html.index(closing_tag, start)]


def test_human_reply_is_stored_unsigned_as_human(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    thread_id = _open_thread(browser, alice)
    _log_in(browser, human_password)

    r = browser.post(f"/t/{thread_id}/reply", data={"body": "Thanks, merging now"}, follow_redirects=False)

    assert r.status_code == 303, f"reply rejected: {r.status_code} {r.text}"
    post = _read(browser, f"/api/threads/{thread_id}")["posts"][-1]
    stored = (post["author"], post["author_kind"], post["verified"], post["signature"], post["body"])
    assert stored == ("human", "human", False, None, "Thanks, merging now"), (
        f"human reply stored wrongly: {post}"
    )


def test_login_sets_session_cookie(browser: TestClient, human_password: str) -> None:
    r = browser.post("/login", data={"password": human_password}, follow_redirects=False)

    assert (r.status_code, r.headers.get("location")) == (303, "/"), f"login failed: {r.status_code} {r.text}"
    name, _, rest = r.headers["set-cookie"].partition("=")
    value, *attributes = (part.strip() for part in rest.split(";"))
    flags = {attribute.lower() for attribute in attributes}
    entropy = len(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    assert (name, entropy) == (SESSION_COOKIE, SESSION_ENTROPY_BYTES), (
        f"wrong session cookie: {r.headers['set-cookie']}"
    )
    expected = {"httponly", "secure", "samesite=lax", f"max-age={THIRTY_DAYS_SECONDS}", "path=/"}
    assert expected <= flags, f"session cookie attributes wrong: {attributes}"
    assert browser.get("/", follow_redirects=False).status_code == 200, "session cookie not accepted"


def test_login_rejects_wrong_password(browser: TestClient) -> None:
    r = browser.post("/login", data={"password": "wrong horse"}, follow_redirects=False)

    assert r.status_code == 401, f"wrong password accepted: {r.status_code}"
    assert "set-cookie" not in r.headers, f"wrong password got a cookie: {r.headers['set-cookie']}"
    assert browser.get("/", follow_redirects=False).status_code == 303, "index served without a session"


def test_logout_clears_session(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)
    session = browser.cookies[SESSION_COOKIE]

    r = browser.post("/logout", follow_redirects=False)

    assert (r.status_code, r.headers.get("location")) == (303, "/login"), f"logout failed: {r.status_code}"
    assert SESSION_COOKIE not in browser.cookies, "logout left the cookie in the browser"
    replayed = browser.get("/", headers={"Cookie": f"{SESSION_COOKIE}={session}"}, follow_redirects=False)
    assert replayed.status_code == 303, f"logged-out session still accepted: {replayed.status_code}"


def test_index_and_thread_pages_render(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    thread_id = _open_thread(browser, alice, title="Rotate the TLS cert", body="<script>alert(1)</script>")
    _send(browser, alice, "post", f"thread:{thread_id}", {"body": "done"})
    _log_in(browser, human_password)

    index = _page(browser, "/")
    thread = _page(browser, f"/t/{thread_id}")

    general = _element(index, "subforum-general", "</li>")
    assert "2 posts" in general, f"general post count missing: {general}"
    assert "Rotate the TLS cert" in thread, f"thread title missing: {thread}"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in thread, "post body not escaped"
    assert "<script>alert(1)" not in thread, "post body rendered as markup"
    assert f'hx-get="/t/{thread_id}/posts"' in thread and 'hx-trigger="every 10s"' in thread, (
        f"open thread does not poll: {thread}"
    )


def test_forum_index_shows_each_subforums_latest_activity(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    _open_thread(browser, alice, title="Nightly backups")
    latest = _open_thread(browser, alice, title="Kernel upgrade")
    _send(browser, alice, "post", f"thread:{latest}", {"body": "rebooted"})
    _log_in(browser, human_password)

    index = _page(browser, "/")

    general = _element(index, "subforum-general", "</li>")
    introductions = _element(index, "subforum-introductions", "</li>")
    assert "2 threads" in general and "3 posts" in general, f"general counts missing: {general}"
    assert f'href="/t/{latest}"' in general and "Kernel upgrade" in general, (
        f"latest thread missing: {general}"
    )
    assert "claude@testbox" in general and "<time" in general, f"latest author or time missing: {general}"
    assert "No posts yet" in introductions and "<time" not in introductions, (
        f"empty subforum not marked: {introductions}"
    )


def test_subforum_page_lists_threads(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    _open_thread(browser, alice, title="Nightly backups")
    _open_thread(browser, alice, title="Kernel upgrade")
    _open_thread(browser, alice, subforum="introductions", title="Only in introductions")
    _log_in(browser, human_password)

    page = _page(browser, "/f/general")

    assert "Nightly backups" in page and "Kernel upgrade" in page, f"general threads missing: {page}"
    assert "Only in introductions" not in page, "thread from another subforum listed"


def test_new_thread_form_opens_thread_as_human(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)

    r = _submit(
        browser, "/f/introductions/threads", title="Who owns the CI runner?", body="@all please answer"
    )

    thread_id = int(r.headers["location"].removeprefix("/t/"))
    thread = _read(browser, f"/api/threads/{thread_id}")
    opening = thread["posts"][0]
    shape = (
        thread["subforum"],
        thread["title"],
        opening["author"],
        opening["author_kind"],
        opening["verified"],
    )
    assert shape == ("introductions", "Who owns the CI runner?", "human", "human", False), (
        f"thread opened wrongly: {thread}"
    )


def test_ticket_board_forms_claim_and_move(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)

    _submit(browser, "/tickets", title="Rotate the TLS cert", body="It expires Friday", column="todo")
    [ticket] = _read(browser, "/api/tickets")
    _submit(browser, f"/tickets/{ticket['id']}/claim")
    _submit(browser, f"/tickets/{ticket['id']}/move", column="doing")

    moved = _read(browser, f"/api/tickets/{ticket['id']}")
    assert (moved["created_by"], moved["assignee"], moved["column"]) == ("human", "human", "doing"), (
        f"ticket forms did not apply: {moved}"
    )
    opening = _read(browser, f"/api/threads/{ticket['thread_id']}")["posts"][0]
    assert (opening["author_kind"], opening["verified"]) == ("human", False), f"opening post wrong: {opening}"
    board = _page(browser, "/tickets")
    doing = _element(board, "column-doing", "</section>")
    assert "Rotate the TLS cert" in doing, f"ticket not in doing: {board}"


def test_ticket_release_form_unassigns(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)
    _submit(browser, "/tickets", title="Rotate the TLS cert", body="It expires Friday")
    [ticket] = _read(browser, "/api/tickets")
    _submit(browser, f"/tickets/{ticket['id']}/claim")

    _submit(browser, f"/tickets/{ticket['id']}/release")

    released = _read(browser, f"/api/tickets/{ticket['id']}")
    assert released["assignee"] is None, f"release form did not unassign: {released}"


def test_files_page_upload_stores_file_as_human(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)
    content = b"hand-off notes for the night shift"

    r = browser.post(
        "/files",
        files={"blob": ("notes.txt", content, "text/plain; charset=utf-8")},
        data={"ttl_days": "2"},
        follow_redirects=False,
    )

    assert r.status_code == 303, f"upload refused: {r.status_code} {r.text}"
    [entry] = _read(browser, "/api/files")
    stored = (entry["name"], entry["content_type"], entry["uploaded_by"], entry["sha256"])
    expected = ("notes.txt", "text/plain", "human", hashlib.sha256(content).hexdigest())
    assert stored == expected, f"human upload stored wrongly: {entry}"
    lifetime = datetime.fromisoformat(entry["expires_at"]) - datetime.fromisoformat(entry["created_at"])
    assert lifetime == timedelta(days=2), f"ttl_days ignored: {entry}"
    assert "notes.txt" in _page(browser, "/files"), "uploaded file not listed on the files page"


def test_human_session_can_download(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    content = b"agent hand-off"
    declaration = {
        "name": "handoff.txt",
        "content_type": "text/plain",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    r = browser.post(
        "/api/files",
        data=_signed_body(*alice.envelope("file.put", "", declaration)),
        files={"blob": ("blob", content, "application/octet-stream")},
    )
    assert r.status_code == 201, f"agent upload refused: {r.status_code} {r.text}"
    _log_in(browser, human_password)

    download = browser.get(f"/api/files/{r.json()['id']}")

    assert (download.status_code, download.content) == (200, content), (
        f"human session cannot download: {download.status_code} {download.text}"
    )


def test_agents_page_shows_online_dot(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    polling, stale = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, polling, stale)
    profile = {"display_name": "Claude", "specialities": ["nginx", "python"], "wake_every": "PT2H"}
    r = _send(browser, polling, "introduce", "", profile)
    assert r.status_code == 200, f"introduce rejected: {r.status_code} {r.text}"
    now = time.time()
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.executemany(
            "INSERT INTO principals (principal, last_seen_at, last_poll_at) VALUES (?, ?, ?) "
            "ON CONFLICT (principal) DO UPDATE SET last_poll_at = excluded.last_poll_at",
            [(polling.principal, now, now - 10), (stale.principal, None, now - 2 * PRESENCE_WINDOW_SECONDS)],
        )
    _log_in(browser, human_password)

    page = _page(browser, "/agents")

    online = _element(page, "agent-claude@testbox", "</tr>")
    offline = _element(page, "agent-pi@testbox", "</tr>")
    assert 'class="dot online"' in online, f"polling agent not shown online: {online}"
    assert 'class="dot offline"' in offline, f"stale agent not shown offline: {offline}"
    assert "nginx, python" in online and "every 2h" in online, f"profile details missing: {online}"
    assert "never" in offline, f"unseen agent shows a last-seen time: {offline}"


def test_closed_thread_partial_returns_286(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    open_thread = _open_thread(browser, alice)
    _log_in(browser, human_password)
    _submit(browser, "/tickets", title="Rotate the TLS cert", body="It expires Friday")
    [ticket] = _read(browser, "/api/tickets")
    _submit(browser, f"/tickets/{ticket['id']}/move", column="done")

    closed = browser.get(f"/t/{ticket['thread_id']}/posts")
    still_open = browser.get(f"/t/{open_thread}/posts")

    assert closed.status_code == 286, f"done ticket thread keeps polling: {closed.status_code}"
    assert "It expires Friday" in closed.text, f"final refresh lost the posts: {closed.text}"
    assert still_open.status_code == 200, f"open thread partial failed: {still_open.status_code}"
    assert "hx-trigger" not in _page(browser, f"/t/{ticket['thread_id']}"), "closed thread page still polls"


@pytest.mark.parametrize("path", PAGES)
def test_unauthenticated_pages_redirect_to_login(browser: TestClient, path: str) -> None:
    r = browser.get(path, follow_redirects=False)

    assert (r.status_code, r.headers.get("location")) == (303, "/login"), (
        f"{path} served without a session: {r.status_code}"
    )


def test_anonymous_reply_is_sent_to_login_and_not_stored(
    browser: TestClient, signer: "SignerFactory"
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    thread_id = _open_thread(browser, alice)

    r = browser.post(f"/t/{thread_id}/reply", data={"body": "anonymous"}, follow_redirects=False)

    assert (r.status_code, r.headers.get("location")) == (303, "/login"), (
        f"anonymous reply not sent to login: {r.status_code}"
    )
    posts = _read_with_bearer(browser, alice, f"/api/threads/{thread_id}")["posts"]
    assert [post["body"] for post in posts] == ["hi"], f"anonymous reply was stored: {posts}"


def test_anonymous_htmx_poll_navigates_to_login(browser: TestClient) -> None:
    r = browser.get("/tickets/columns", headers={"HX-Request": "true"}, follow_redirects=False)

    assert (r.status_code, r.headers.get("hx-redirect")) == (401, "/login"), (
        f"anonymous htmx poll not sent to login: {r.status_code} {r.headers}"
    )


def test_anonymous_api_read_is_refused(browser: TestClient) -> None:
    r = browser.get("/api/subforums")

    assert (r.status_code, r.json()) == (401, {"reason": "bad_token"}), f"anonymous API read: {r.text}"


@pytest.mark.parametrize("path", ["/", "/tickets", "/files", "/agents", "/stats"])
def test_every_page_links_to_stats(browser: TestClient, human_password: str, path: str) -> None:
    _log_in(browser, human_password)

    nav = _page(browser, path).split("<nav>", 1)[-1].split("</nav>", 1)[0]

    assert re.search(r'<a href="/stats"[^>]*>Stats</a>', nav), f"{path} nav has no stats link: {nav}"


@pytest.mark.parametrize("asset", ["favicon.svg", "favicon.ico", "apple-touch-icon.png", "logo-dark.svg"])
def test_brand_assets_are_linked_and_served(browser: TestClient, asset: str) -> None:
    page = _page(browser, "/login")

    served = browser.get(f"/static/{asset}")

    assert f'"/static/{asset}"' in page, f"/login does not link {asset}"
    assert served.status_code == 200 and served.content, f"{asset} not served: {served.status_code}"


def test_login_refuses_with_429_after_repeated_failures(browser: TestClient, human_password: str) -> None:
    statuses = [browser.post("/login", data={"password": "wrong horse"}).status_code for _ in range(5)]
    assert statuses == [401] * 5, f"early failures not plain 401s: {statuses}"

    r = browser.post("/login", data={"password": human_password}, follow_redirects=False)

    assert r.status_code == 429, f"login not throttled after five failures: {r.status_code}"
    assert int(r.headers["retry-after"]) > 0, f"no Retry-After on 429: {r.headers}"
    assert "set-cookie" not in r.headers, "throttled login still got a session"


def test_login_throttle_is_per_address_below_the_global_cap(settings: Settings, human_password: str) -> None:
    app = create_app(settings)
    with (
        TestClient(app, base_url="https://testserver", client=("192.0.2.1", 1)) as guesser,
        TestClient(app, base_url="https://testserver", client=("192.0.2.2", 1)) as owner,
    ):
        for _ in range(5):
            guesser.post("/login", data={"password": "wrong horse"})

        r = owner.post("/login", data={"password": human_password}, follow_redirects=False)

    assert r.status_code == 303, f"another address was throttled by one guesser: {r.status_code}"


def test_login_throttle_has_a_global_cap(settings: Settings, human_password: str) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver") as last:
        for n in range(20):
            with TestClient(app, base_url="https://testserver", client=(f"192.0.2.{n + 1}", 1)) as guesser:
                guesser.post("/login", data={"password": "wrong horse"})

        r = last.post("/login", data={"password": human_password}, follow_redirects=False)

    assert r.status_code == 429, f"distributed guessing not capped: {r.status_code}"


def test_failed_logins_forget_failures_after_the_window() -> None:
    from board.web.auth import FailedLogins

    now = [1000.0]
    limiter = FailedLogins(per_address=2, overall=10, window_seconds=60, clock=lambda: now[0])
    limiter.record("a")
    limiter.record("a")
    assert limiter.retry_after("a") > 0, "limit not reached after two failures"

    now[0] += 61

    assert limiter.retry_after("a") == 0, "failures outlived their window"


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ],
)
def test_cross_site_form_posts_are_refused(
    browser: TestClient, human_password: str, headers: dict[str, str]
) -> None:
    _log_in(browser, human_password)

    r = browser.post(
        "/tickets",
        data={"title": "Forged", "body": "from elsewhere"},
        headers=headers,
        follow_redirects=False,
    )

    assert (r.status_code, r.json()) == (403, {"reason": "cross_site"}), (
        f"forged post: {r.status_code} {r.text}"
    )
    assert _read(browser, "/api/tickets") == [], "cross-site post was stored"


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-origin", "Origin": "https://testserver"},
        {"Sec-Fetch-Site": "none"},
        {"Origin": "https://board.example.com"},
        {"Origin": "https://testserver"},
    ],
)
def test_same_origin_form_posts_are_accepted(
    browser: TestClient, human_password: str, headers: dict[str, str]
) -> None:
    _log_in(browser, human_password)

    r = browser.post(
        "/tickets", data={"title": "Real", "body": "from the board"}, headers=headers, follow_redirects=False
    )

    assert r.status_code == 303, f"same-origin post refused: {r.status_code} {r.text}"


def test_cross_site_login_is_refused(browser: TestClient, human_password: str) -> None:
    r = browser.post(
        "/login",
        data={"password": human_password},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )

    assert r.status_code == 403, f"cross-site login accepted: {r.status_code}"


def test_claiming_a_held_ticket_returns_to_the_board_with_a_banner(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    _log_in(browser, human_password)
    _submit(browser, "/tickets", title="Rotate the TLS cert", body="It expires Friday")
    [ticket] = _read(browser, "/api/tickets")
    claimed = _send(browser, alice, "ticket.claim", f"ticket:{ticket['id']}", {})
    assert claimed.status_code == 200, f"agent claim refused: {claimed.text}"

    r = _submit(browser, f"/tickets/{ticket['id']}/claim")

    assert r.headers["location"] == "/tickets?error=already_claimed", f"wrong redirect: {r.headers}"
    banner = _element(_page(browser, r.headers["location"]), "refusal", "</p>")
    assert 'role="alert"' in banner and "already claimed" in banner, f"banner missing: {banner}"


@pytest.mark.parametrize(
    ("path", "fields", "reason"),
    [
        ("/tickets/{id}/move", {"column": "bogus"}, "bad_payload"),
        ("/tickets/999/claim", {}, "no_such_ticket"),
        ("/tickets/999/release", {}, "no_such_ticket"),
    ],
)
def test_ticket_form_refusals_redirect_with_reason(
    browser: TestClient, human_password: str, path: str, fields: dict[str, str], reason: str
) -> None:
    _log_in(browser, human_password)
    _submit(browser, "/tickets", title="Rotate the TLS cert", body="It expires Friday")
    [ticket] = _read(browser, "/api/tickets")

    r = _submit(browser, path.format(id=ticket["id"]), **fields)

    assert r.headers["location"] == f"/tickets?error={reason}", f"wrong redirect: {r.headers}"
    assert 'id="refusal"' in _page(browser, r.headers["location"]), "no banner for the refusal"


def test_unknown_error_codes_show_no_banner(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)

    page = _page(browser, "/tickets?error=<script>")

    assert 'id="refusal"' not in page and "<script>" not in page, "unknown error code rendered"


def _attached_thread(client: TestClient, author: "Signer", content: bytes) -> tuple[int, str]:
    declaration = {
        "name": "notes.txt",
        "content_type": "text/plain",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    r = client.post(
        "/api/files",
        data=_signed_body(*author.envelope("file.put", "", declaration)),
        files={"blob": ("blob", content, "application/octet-stream")},
    )
    assert r.status_code == 201, f"upload refused: {r.status_code} {r.text}"
    file_id: str = r.json()["id"]
    posted = _send(
        client, author, "post", "subforum:general", {"title": "With file", "body": "see", "files": [file_id]}
    )
    assert posted.status_code == 201, f"post refused: {posted.text}"
    thread_id: int = posted.json()["thread_id"]
    return thread_id, file_id


def test_thread_attachments_show_name_and_size(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    thread_id, file_id = _attached_thread(browser, alice, b"hello!")
    _log_in(browser, human_password)

    attachments = _element(_page(browser, f"/t/{thread_id}"), f"attachment-{file_id}", "</li>")

    assert "notes.txt" in attachments and "6 Bytes" in attachments, f"attachment not named: {attachments}"
    assert f'href="/api/files/{file_id}"' in attachments, f"attachment link lost: {attachments}"


def test_expired_attachment_falls_back_to_its_id(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    alice = signer("claude@testbox")
    _register(browser, alice)
    thread_id, file_id = _attached_thread(browser, alice, b"hello!")
    db_path = _app(browser).state.settings.db_path
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE files SET expires_at = 0")
    _log_in(browser, human_password)

    attachments = _element(_page(browser, f"/t/{thread_id}"), f"attachment-{file_id}", "</li>")

    assert file_id in attachments and "notes.txt" not in attachments, f"expired file shown: {attachments}"


STATIC = Path(__file__).resolve().parents[1] / "src" / "board" / "web" / "static"


@pytest.mark.parametrize(
    ("path", "current"),
    [("/", "/"), ("/f/general", "/"), ("/tickets", "/tickets"), ("/files", "/files"), ("/stats", "/stats")],
)
def test_nav_marks_only_the_current_section(
    browser: TestClient, human_password: str, path: str, current: str
) -> None:
    _log_in(browser, human_password)

    nav = _page(browser, path).split("<nav", 1)[-1].split("</nav>", 1)[0]

    marked = re.findall(r'<a href="([^"]+)" aria-current="page"', nav)
    assert marked == [current], f"{path} marks {marked} as current, expected [{current}]: {nav}"


def test_ticket_card_moves_through_one_labelled_select(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)
    _submit(browser, "/tickets", title="Rotate the TLS cert", body="It expires Friday", column="todo")
    [ticket] = _read(browser, "/api/tickets")

    card = _element(_page(browser, "/tickets"), f"ticket-{ticket['id']}", "</article>")

    move = card.split('class="move"', 1)[-1]
    assert f'<select name="column" aria-label="Move #{ticket["id"]} to"' in move, f"no move select: {card}"
    options = re.findall(r'<option value="([^"]+)"', move)
    assert options == ["backlog", "doing", "review", "done"], f"move targets wrong: {options}"
    assert 'name="column" value=' not in card, f"card still has one button per column: {card}"


def test_new_ticket_form_is_collapsed_above_the_board(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)

    board = _page(browser, "/tickets")

    compose = re.search(
        r'<details class="compose">\s*<summary>New ticket</summary>.*?</details>', board, re.DOTALL
    )
    assert compose and 'action="/tickets"' in compose.group(0), f"new ticket form not collapsed: {board}"
    assert board.index("<h1>Tickets</h1>") < compose.start() < board.index('id="columns"'), (
        f"compose not between heading and board: {board}"
    )


def test_header_uses_the_mark_alone_on_narrow_screens(browser: TestClient) -> None:
    page = _page(browser, "/login")

    picture = re.search(r"<picture>.*?</picture>", page, re.DOTALL)
    assert picture, f"header has no responsive logo: {page}"
    assert '<source media="(max-width: 40rem)" srcset="/static/mark-orange.svg"' in picture.group(0), (
        f"narrow screens do not get the mark: {picture.group(0)}"
    )
    served = browser.get("/static/mark-orange.svg")
    assert served.status_code == 200 and b"<svg" in served.content, f"mark not served: {served.status_code}"


@pytest.mark.parametrize("path", ["/f/general", "/files", "/agents"])
def test_listing_tables_scroll_inside_their_own_box(
    browser: TestClient, human_password: str, path: str
) -> None:
    _log_in(browser, human_password)

    page = _page(browser, path)

    tables = page.count('<table class="listing">')
    wrapped = len(re.findall(r'<div class="table-scroll">\s*<table class="listing">', page))
    assert tables and wrapped == tables, f"{path}: {wrapped} of {tables} listing tables wrapped"


@pytest.mark.parametrize(
    "rule",
    [
        "color-scheme: light dark",
        "@media (prefers-color-scheme: dark)",
        "--signal: #e26643",
        ":focus-visible",
        "@media (pointer: coarse)",
        "@media (max-width: 40rem)",
        ".stats-tile",
        'nav a[aria-current="page"]',
    ],
)
def test_stylesheet_declares(rule: str) -> None:
    css = (STATIC / "board.css").read_text()

    assert rule in css, f"board.css lacks {rule!r}"
