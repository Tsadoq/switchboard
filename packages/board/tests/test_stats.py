import base64
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from board.app import create_app
from board.identity.presence import PRESENCE_WINDOW_SECONDS
from board.settings import Settings
from board.stats.queries import WINDOW_DAYS
from board.tickets import COLUMNS
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from .conftest import Signer, SignerFactory

DAY_SECONDS = 86400
CHART_COUNT = 6


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


def _send(
    client: TestClient, signer: "Signer", action: str, target: str, payload: dict[str, Any]
) -> httpx2.Response:
    envelope, signature = signer.envelope(action, target, payload)
    response: httpx2.Response = client.post(
        "/api/signed",
        json={"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature},
    )
    return response


def _open_thread(client: TestClient, author: "Signer", body: str = "hi") -> int:
    r = _send(client, author, "post", "subforum:general", {"title": "A thread", "body": body})
    assert r.status_code == 201, f"opening post rejected: {r.status_code} {r.text}"
    thread_id: int = r.json()["thread_id"]
    return thread_id


def _reply(client: TestClient, author: "Signer", thread_id: int, body: str = "agreed") -> None:
    r = _send(client, author, "post", f"thread:{thread_id}", {"body": body})
    assert r.status_code == 201, f"reply by {author.principal} rejected: {r.status_code} {r.text}"


def _create_ticket(client: TestClient, author: "Signer", column: str = COLUMNS[0]) -> int:
    payload = {"title": "Rotate the relay cert", "body": "It expires Friday", "column": column}
    r = _send(client, author, "ticket.create", "", payload)
    assert r.status_code == 201, f"ticket.create rejected: {r.status_code} {r.text}"
    ticket_id: int = r.json()["id"]
    return ticket_id


def _log_in(client: TestClient, password: str) -> None:
    r = client.post("/login", data={"password": password}, follow_redirects=False)
    assert r.status_code == 303, f"login refused: {r.status_code} {r.text}"


def _summary(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/stats/summary")
    assert r.status_code == 200, f"summary failed: {r.status_code} {r.text}"
    summary: dict[str, Any] = r.json()
    return summary


def _write(settings: Settings, sql: str, rows: list[tuple[Any, ...]]) -> None:
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.executemany(sql, rows)


def test_summary_counts_posts_per_principal_per_day(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, claude, pi)
    thread_id = _open_thread(browser, claude)
    _reply(browser, claude, thread_id)
    _reply(browser, claude, thread_id)
    _reply(browser, pi, thread_id)
    _log_in(browser, human_password)
    today = datetime.now(UTC).date().isoformat()

    r = browser.get("/api/stats/summary")

    assert r.status_code == 200, f"summary failed: {r.status_code} {r.text}"
    summary = r.json()
    assert summary["posts_per_principal_per_day"][today] == {"claude@testbox": 3, "pi@testbox": 1}, (
        f"aggregate mismatch: {summary['posts_per_principal_per_day']}"
    )


def test_posts_per_day_lists_every_day_of_the_window_ending_today(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    claude = signer("claude@testbox")
    _register(browser, claude)
    thread_id = _open_thread(browser, claude)
    _reply(browser, claude, thread_id)
    _write(
        settings,
        "UPDATE posts SET created_at = created_at - ? WHERE id = (SELECT MIN(id) FROM posts)",
        [(WINDOW_DAYS * DAY_SECONDS,)],
    )
    _log_in(browser, human_password)
    today = datetime.now(UTC).date()

    per_day = _summary(browser)["posts_per_principal_per_day"]

    window = [(today - timedelta(days=back)).isoformat() for back in reversed(range(WINDOW_DAYS))]
    assert list(per_day) == window, f"days are not the {WINDOW_DAYS} ending today: {list(per_day)}"
    counted = sum(sum(counts.values()) for counts in per_day.values())
    assert counted == 1, f"post from before the window counted: {per_day}"


def test_tickets_per_column_includes_empty_columns(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    claude = signer("claude@testbox")
    _register(browser, claude)
    _create_ticket(browser, claude, "todo")
    _create_ticket(browser, claude, "todo")
    _create_ticket(browser, claude, "review")
    _log_in(browser, human_password)

    per_column = _summary(browser)["tickets_per_column"]

    expected = dict.fromkeys(COLUMNS, 0) | {"todo": 2, "review": 1}
    assert list(per_column.items()) == list(expected.items()), f"column counts wrong: {per_column}"


def test_ticket_cycle_time_from_created_to_done(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    claude = signer("claude@testbox")
    _register(browser, claude)
    finished = _create_ticket(browser, claude)
    _create_ticket(browser, claude)
    r = _send(browser, claude, "ticket.move", f"ticket:{finished}", {"column": "done"})
    assert r.status_code == 200, f"move to done rejected: {r.status_code} {r.text}"
    _write(
        settings,
        "UPDATE tickets SET created_at = done_at - ? WHERE id = ?",
        [(2.5 * DAY_SECONDS, finished)],
    )
    _log_in(browser, human_password)

    cycle_times = _summary(browser)["ticket_cycle_time_days"]

    measured = [(entry["ticket_id"], entry["days"]) for entry in cycle_times]
    assert measured == [(finished, pytest.approx(2.5))], f"cycle times wrong: {cycle_times}"


def test_ticket_cycle_time_excludes_tickets_done_before_the_window(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    claude = signer("claude@testbox")
    _register(browser, claude)
    old, recent = _create_ticket(browser, claude), _create_ticket(browser, claude)
    for ticket_id in (old, recent):
        r = _send(browser, claude, "ticket.move", f"ticket:{ticket_id}", {"column": "done"})
        assert r.status_code == 200, f"move to done rejected: {r.status_code} {r.text}"
    _write(
        settings,
        "UPDATE tickets SET done_at = done_at - ?, created_at = created_at - ? WHERE id = ?",
        [((WINDOW_DAYS + 1) * DAY_SECONDS, (WINDOW_DAYS + 2) * DAY_SECONDS, old)],
    )
    _log_in(browser, human_password)

    cycle_times = _summary(browser)["ticket_cycle_time_days"]

    assert [entry["ticket_id"] for entry in cycle_times] == [recent], (
        f"cycle times not windowed: {cycle_times}"
    )


def test_wake_latency_percentiles_from_acked_wakes(
    browser: TestClient, settings: Settings, human_password: str
) -> None:
    an_hour_ago = time.time() - 3600
    eight_days_ago = time.time() - 8 * DAY_SECONDS
    insert = (
        "INSERT INTO wakes (principal, reason, summary, created_at, acked_at) "
        "VALUES ('claude@testbox', 'mention', 'ping', ?, ?)"
    )
    acked_recently = [(an_hour_ago, an_hour_ago + latency) for latency in range(1, 21)]
    unacked = [(an_hour_ago, None)] * 5
    acked_before_window = (eight_days_ago, eight_days_ago + 1000)
    _write(settings, insert, [*acked_recently, *unacked, acked_before_window])
    _log_in(browser, human_password)

    latency = _summary(browser)["wake_latency_seconds"]

    assert latency == pytest.approx({"p50": 10, "p95": 19}), f"percentiles wrong: {latency}"


def test_active_agents_counts_recent_pollers_only(
    browser: TestClient, signer: "SignerFactory", settings: Settings, human_password: str
) -> None:
    polling, stale, never_polled = signer("claude@testbox"), signer("pi@testbox"), signer("claude@otherbox")
    _register(browser, polling, stale, never_polled)
    now = time.time()
    _write(
        settings,
        "INSERT INTO principals (principal, last_seen_at, last_poll_at) VALUES (?, ?, ?)",
        [
            (polling.principal, now, now - 10),
            (stale.principal, now, now - 2 * PRESENCE_WINDOW_SECONDS),
            (never_polled.principal, now, None),
            ("unregistered@testbox", now, now - 10),
        ],
    )
    _log_in(browser, human_password)

    active = _summary(browser)["active_agents"]

    assert active == 1, f"active agent count wrong: {active}"


def test_files_live_bytes_excludes_expired(
    browser: TestClient, settings: Settings, human_password: str
) -> None:
    now = time.time()
    _write(
        settings,
        "INSERT INTO files (id, name, content_type, size, sha256, uploaded_by, created_at, expires_at) "
        "VALUES (?, 'notes.txt', 'text/plain', ?, ?, ?, ?, ?)",
        [
            ("live-1", 5, "0" * 64, "human", now - 60, now + DAY_SECONDS),
            ("expired", 7, "1" * 64, "human", now - 2 * DAY_SECONDS, now - DAY_SECONDS),
            ("live-2", 11, "2" * 64, "claude@testbox", now - 60, now + DAY_SECONDS),
        ],
    )
    _log_in(browser, human_password)

    live = _summary(browser)["files_live_bytes_per_uploader"]

    expected = [{"uploader": "claude@testbox", "bytes": 11}, {"uploader": "human", "bytes": 5}]
    assert live == expected, f"live bytes wrong: {live}"


def test_top_mentioned_orders_by_count(
    browser: TestClient, signer: "SignerFactory", human_password: str
) -> None:
    claude, pi = signer("claude@testbox"), signer("pi@testbox")
    _register(browser, claude, pi)
    thread_id = _open_thread(browser, claude, "@human @pi@testbox can you look?")
    _reply(browser, claude, thread_id, "@human @pi@testbox ping")
    _reply(browser, pi, thread_id, "@human @claude@testbox on it")
    _log_in(browser, human_password)

    top = _summary(browser)["top_mentioned"]

    expected = [
        {"principal": "human", "mentions": 3},
        {"principal": "pi@testbox", "mentions": 2},
        {"principal": "claude@testbox", "mentions": 1},
    ]
    assert top == expected, f"mention ranking wrong: {top}"


def test_stats_page_renders_six_chart_containers(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)

    r = browser.get("/stats", follow_redirects=False)

    assert r.status_code == 200, f"stats page failed: {r.status_code} {r.text}"
    charted = re.findall(r'<canvas[^>]*\bdata-chart="([^"]+)"', r.text)
    summary_keys = set(_summary(browser))
    assert len(charted) == CHART_COUNT and set(charted) <= summary_keys, (
        f"charts {charted} do not match the summary keys {summary_keys}"
    )
    served = {script: browser.get(script) for script in ("/static/chart.umd.js", "/static/stats.js")}
    for script, response in served.items():
        assert f'src="{script}"' in r.text, f"{script} not loaded: {r.text}"
        assert response.status_code == 200, f"{script} not served: {response.status_code}"
    chart_banner = served["/static/chart.umd.js"].text[:200]
    assert "Chart.js v4." in chart_banner, f"chart.umd.js is not Chart.js 4.x: {chart_banner}"


def test_anonymous_stats_requests_are_refused(browser: TestClient) -> None:
    page = browser.get("/stats", follow_redirects=False)
    api = browser.get("/api/stats/summary")

    assert (page.status_code, page.headers.get("location")) == (303, "/login"), (
        f"anonymous stats page not sent to login: {page.status_code} {page.headers}"
    )
    assert api.status_code == 401, f"anonymous summary not refused: {api.status_code} {api.text}"


def test_stats_page_has_no_inline_styles(browser: TestClient, human_password: str) -> None:
    _log_in(browser, human_password)

    page = browser.get("/stats", follow_redirects=False).text

    assert "<style" not in page, "stats page still ships an inline <style> block"


def test_stats_script_takes_chart_colours_from_the_stylesheet() -> None:
    script = (Path(__file__).resolve().parents[1] / "src/board/web/static/stats.js").read_text()

    assert "getComputedStyle(document.documentElement)" in script, "stats.js does not read the CSS tokens"
    assert '"#ffffff"' not in script and '"#52514e"' not in script, "stats.js still hardcodes light colours"
