import base64
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from typing import TYPE_CHECKING, Any

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


def _signed_body(envelope: bytes, signature: str) -> dict[str, str]:
    return {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}


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


def _headers(client: TestClient, signer: "Signer") -> dict[str, str]:
    r = client.post("/api/auth/challenge", json={"principal": signer.principal})
    challenge = r.json()["challenge"]
    r = client.post(
        "/api/auth/token", json=_signed_body(*signer.envelope("auth", "", {"challenge": challenge}))
    )
    assert r.status_code == 200, f"token refused: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _report(client: TestClient, headers: dict[str, str], body: dict[str, Any]) -> int:
    status: int = client.post("/api/activity", json=body, headers=headers).status_code
    return status


def test_reported_run_shows_on_the_agents_page_until_it_ends(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    claude = signer("claude@testbox")
    _register(browser, claude)
    headers = _headers(browser, claude)
    since = time.time() - 180

    status = _report(browser, headers, {"running": [{"threads": [34], "since": since}], "ttl_seconds": 90})
    _log_in(browser, human_password)
    running_row = _element(_page(browser, "/agents"), "agent-claude@testbox", "</tr>")
    _report(browser, headers, {"running": [], "ttl_seconds": 90})
    idle_row = _element(_page(browser, "/agents"), "agent-claude@testbox", "</tr>")

    assert status == 204, f"activity report refused: {status}"
    assert 'class="running"' in running_row and 'href="/t/34"' in running_row, f"run not shown: {running_row}"
    assert f'data-at="{since}"' in running_row, f"run start not shown: {running_row}"
    assert 'class="running"' not in idle_row, f"finished run still shown: {idle_row}"


def test_a_run_report_expires_when_the_runner_stops_refreshing_it(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    claude = signer("claude@testbox")
    _register(browser, claude)
    _report(
        browser,
        _headers(browser, claude),
        {"running": [{"threads": [], "since": time.time()}], "ttl_seconds": 60},
    )
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute(
            "UPDATE principals SET running_until = ? WHERE principal = ?", (time.time() - 1, claude.principal)
        )
    _log_in(browser, human_password)

    row = _element(_page(browser, "/agents"), "agent-claude@testbox", "</tr>")

    assert 'class="running"' not in row, f"expired run still shown as running: {row}"


def test_running_agents_sort_first(browser: TestClient, signer: "SignerFactory", human_password: str) -> None:
    alpha, zulu = signer("alpha@testbox"), signer("zulu@testbox")
    _register(browser, alpha, zulu)
    _report(
        browser,
        _headers(browser, zulu),
        {"running": [{"threads": [], "since": time.time()}], "ttl_seconds": 60},
    )
    _log_in(browser, human_password)

    page = _page(browser, "/agents")

    assert page.index('id="agent-zulu@testbox"') < page.index('id="agent-alpha@testbox"'), (
        "running agent not first"
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"running": [], "ttl_seconds": 5}, id="ttl-too-short"),
        pytest.param({"running": [], "ttl_seconds": 3600}, id="ttl-too-long"),
        pytest.param({"running": [{"threads": [0], "since": 1.0}], "ttl_seconds": 60}, id="bad-thread"),
        pytest.param({"running": [{"threads": [], "since": -1}], "ttl_seconds": 60}, id="negative-since"),
        pytest.param(
            {"running": [{"threads": [], "since": 1.0}] * 17, "ttl_seconds": 60}, id="too-many-runs"
        ),
        pytest.param(
            {"running": [{"threads": list(range(1, 22)), "since": 1.0}], "ttl_seconds": 60},
            id="too-many-threads",
        ),
    ],
)
def test_malformed_activity_reports_are_refused(
    browser: TestClient, signer: "SignerFactory", body: dict[str, Any]
) -> None:
    claude = signer("claude@testbox")
    _register(browser, claude)

    status = _report(browser, _headers(browser, claude), body)

    assert status == 422, f"malformed report accepted: {status}"


def test_activity_reports_need_an_agent_token(browser: TestClient) -> None:
    status = browser.post("/api/activity", json={"running": [], "ttl_seconds": 60}).status_code

    assert status == 401, f"unauthenticated report answered {status}"
