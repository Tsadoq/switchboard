import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from board.app import create_app
from board.settings import Settings
from fastapi.testclient import TestClient

INSTALLED = "a" * 40
LATEST = "c" * 40
MIDDLE = "b" * 40


@pytest.fixture
def browser(settings: Settings, human_password: str) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as browser:
        r = browser.post("/login", data={"password": human_password}, follow_redirects=False)
        assert r.status_code == 303, f"login refused: {r.status_code} {r.text}"
        yield browser


@pytest.fixture
def request_dir(settings: Settings) -> Path:
    return settings.state_dir / "update"


def _commit(sha: str, subject: str) -> dict[str, str]:
    return {"commit": sha, "short": sha[:12], "subject": subject, "date": "2026-11-08T10:00:00+00:00"}


def _write_status(settings: Settings, **overrides: Any) -> None:
    status = {
        "version": 1,
        "state": "idle",
        "remote": "https://github.com/Tsadoq/switchboard.git",
        "branch": "main",
        "checked_at": 1_800_000_000.0,
        "installed": _commit(INSTALLED, "Installed release"),
        "latest": _commit(LATEST, "Add the updates page"),
        "behind": 2,
        "pending": [_commit(LATEST, "Add the updates page"), _commit(MIDDLE, "Fix <b>escaping</b>")],
        "last_update": None,
        "error": None,
        "requested": None,
        "log_tail": [],
    }
    settings.update_status_path.write_text(json.dumps(status | overrides))


def _page(browser: TestClient) -> str:
    r = browser.get("/updates", follow_redirects=False)
    assert r.status_code == 200, f"updates page failed: {r.status_code} {r.text}"
    html: str = r.text
    return html


def test_page_explains_when_no_updater_is_installed(browser: TestClient, request_dir: Path) -> None:
    html = _page(browser)

    r = browser.post("/updates/check", follow_redirects=False)

    assert "cannot update itself" in html, f"no explanation without an updater: {html}"
    assert r.headers["location"] == "/updates?error=unavailable", f"check accepted: {r.headers}"
    assert not request_dir.exists(), "a request was left for an updater that does not exist"


def test_page_lists_new_commits_and_offers_the_latest(browser: TestClient, settings: Settings) -> None:
    _write_status(settings)

    html = _page(browser)

    assert "2 new commits" in html, f"pending count missing: {html}"
    assert "Fix &lt;b&gt;escaping&lt;/b&gt;" in html, "commit subject not escaped"
    assert f'name="commit" value="{LATEST}"' in html, f"latest not offered: {html}"
    assert 'title="2 new commits">2</span>' in html, f"nav badge missing: {html}"
    assert "hx-trigger" not in html, "an idle page polls"


def test_up_to_date_board_offers_nothing(browser: TestClient, settings: Settings) -> None:
    _write_status(settings, installed=_commit(LATEST, "Add the updates page"), behind=0, pending=[])

    html = _page(browser)

    assert "Up to date" in html, f"up to date not shown: {html}"
    assert "/updates/apply" not in html, "an up to date board offers an update"


def test_apply_leaves_a_request_naming_the_commit(
    browser: TestClient, settings: Settings, request_dir: Path
) -> None:
    _write_status(settings)

    r = browser.post("/updates/apply", data={"commit": LATEST}, follow_redirects=False)
    html = _page(browser)

    assert r.headers["location"] == "/updates", f"apply refused: {r.status_code} {r.headers}"
    assert (request_dir / "apply").read_text() == f"{LATEST}\n", "request does not name the commit"
    assert sorted(path.name for path in request_dir.iterdir()) == ["apply"], "temporary file left behind"
    assert 'hx-get="/updates/panel"' in html and "Updating" in html, (
        f"page does not follow the update: {html}"
    )
    assert "/updates/apply" not in html, "update offered again while one is queued"


def test_apply_refuses_a_commit_that_is_not_on_offer(
    browser: TestClient, settings: Settings, request_dir: Path
) -> None:
    _write_status(settings)

    r = browser.post("/updates/apply", data={"commit": MIDDLE}, follow_redirects=False)

    assert r.headers["location"] == "/updates?error=not_offered", f"other commit accepted: {r.headers}"
    assert not (request_dir / "apply").exists(), "request left for a commit not on offer"


def test_apply_refuses_while_the_updater_is_busy(
    browser: TestClient, settings: Settings, request_dir: Path
) -> None:
    _write_status(settings, state="checking")

    r = browser.post("/updates/apply", data={"commit": LATEST}, follow_redirects=False)

    assert r.headers["location"] == "/updates?error=not_offered", f"apply accepted while busy: {r.headers}"
    assert not (request_dir / "apply").exists(), "request left while the updater runs"


def test_check_leaves_a_request(browser: TestClient, settings: Settings, request_dir: Path) -> None:
    _write_status(settings)

    r = browser.post("/updates/check", follow_redirects=False)

    assert r.headers["location"] == "/updates", f"check refused: {r.headers}"
    assert (request_dir / "check").exists(), "no check request"
    assert "Checking for updates" in _page(browser), "queued check not shown"


def test_panel_stops_polling_once_the_updater_is_idle(browser: TestClient, settings: Settings) -> None:
    _write_status(settings, state="applying", requested=LATEST, log_tail=["==> installing"])
    busy = browser.get("/updates/panel").text
    _write_status(settings, last_update={"ok": True, "to": LATEST, "finished_at": 1_800_000_100.0})

    idle = browser.get("/updates/panel").text

    assert 'hx-trigger="every 3s"' in busy and "==&gt; installing" in busy, f"busy panel wrong: {busy}"
    assert "hx-trigger" not in idle and f"Updated to <code>{LATEST[:12]}</code>" in idle, (
        f"idle panel wrong: {idle}"
    )


def test_failed_update_is_reported(browser: TestClient, settings: Settings) -> None:
    last = {"ok": False, "to": LATEST, "rolled_back": True, "finished_at": 1.0, "message": "healthz failed"}
    _write_status(settings, last_update=last, error="the update failed")

    html = _page(browser)

    assert "failed and was rolled back" in html and "healthz failed" in html, f"failure not shown: {html}"
    assert f"Retry update to {LATEST[:12]}" in html, f"failed update not offered as a retry: {html}"


def test_malformed_status_does_not_break_the_page(browser: TestClient, settings: Settings) -> None:
    _write_status(settings, behind="many", pending="none", installed={"commit": "nope"}, log_tail=7)

    html = _page(browser)

    assert "/updates/apply" not in html, f"malformed status offered an update: {html}"


def test_updates_need_a_login(settings: Settings) -> None:
    _write_status(settings)
    with TestClient(create_app(settings), base_url="https://testserver") as anonymous:
        page = anonymous.get("/updates", follow_redirects=False)
        posted = anonymous.post("/updates/apply", data={"commit": LATEST}, follow_redirects=False)

    assert (page.status_code, page.headers["location"]) == (303, "/login"), "page served without login"
    assert (posted.status_code, posted.headers["location"]) == (303, "/login"), "apply without login"
    assert not (settings.state_dir / "update" / "apply").exists(), "anonymous request stored"


def test_cross_site_apply_is_refused(browser: TestClient, settings: Settings, request_dir: Path) -> None:
    _write_status(settings)

    r = browser.post(
        "/updates/apply",
        data={"commit": LATEST},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )

    assert r.status_code == 403, f"cross-site apply accepted: {r.status_code}"
    assert not (request_dir / "apply").exists(), "cross-site request stored"
