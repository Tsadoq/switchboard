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


def _seed_paged_stats(settings: Settings, count: int = 105) -> None:
    from uuid import UUID

    now = time.time()
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        for i in range(count):
            principal = f"worker{i:03}@box"
            thread_id = conn.execute(
                "INSERT INTO threads (subforum, title, kind, created_by, created_at) "
                "VALUES ('general', ?, 'ticket', 'human', ?)",
                (f"Ticket {i:03}", now - DAY_SECONDS),
            ).lastrowid
            conn.execute(
                "INSERT INTO posts (thread_id, author, author_kind, body, created_at) "
                "VALUES (?, ?, 'agent', 'Post', ?)",
                (thread_id, principal, now),
            )
            conn.execute(
                'INSERT INTO tickets (title, "column", thread_id, created_by, created_at, updated_at, done_at) '
                "VALUES (?, 'done', ?, 'human', ?, ?, ?)",
                (f"Ticket {i:03}", thread_id, now - DAY_SECONDS, now, now),
            )
            conn.execute(
                "INSERT INTO files (id, name, content_type, size, sha256, uploaded_by, created_at, expires_at) "
                "VALUES (?, 'Sample', 'text/plain', ?, ?, ?, ?, ?)",
                (str(UUID(int=i + 1)), i + 1, "0" * 64, principal, now, now + 3600),
            )


def test_stats_chart_data_is_paged_without_changing_summary_api(
    browser: TestClient, settings: Settings, human_password: str
) -> None:
    _log_in(browser, human_password)
    _seed_paged_stats(settings)
    response = browser.get("/stats/data")
    assert response.status_code == 200
    first = response.json()
    for key in ("posts", "tickets", "files"):
        assert first["pages"][key]["total"] == 105
        assert first["pages"][key]["shown"] == 50
    assert len(first["ticket_cycle_time_days"]) == len(first["files_live_bytes_per_uploader"]) == 50
    assert [row["ticket_id"] for row in first["ticket_cycle_time_days"]] == list(range(105, 55, -1))
    assert len({name for day in first["posts_per_principal_per_day"].values() for name in day}) == 50
    params = {
        "posts_after": first["pages"]["posts"]["next"],
        "tickets_before": first["pages"]["tickets"]["next"],
        "files_after": first["pages"]["files"]["next"],
    }
    middle = browser.get("/stats/data", params=params).json()
    assert [row["ticket_id"] for row in middle["ticket_cycle_time_days"]] == list(range(55, 5, -1))
    assert middle["files_live_bytes_per_uploader"][0]["uploader"] == "worker054@box"
    back = browser.get("/stats/data", params={"tickets_after": middle["pages"]["tickets"]["previous"]}).json()
    assert back["ticket_cycle_time_days"] == first["ticket_cycle_time_days"]
    legacy = _summary(browser)
    assert "pages" not in legacy
    assert len(legacy["ticket_cycle_time_days"]) == len(legacy["files_live_bytes_per_uploader"]) == 105


def test_stats_cursors_survive_deleted_boundaries_and_expiry(
    browser: TestClient, settings: Settings, human_password: str
) -> None:
    _log_in(browser, human_password)
    _seed_paged_stats(settings)
    first = browser.get("/stats/data").json()
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("DELETE FROM tickets WHERE id = 56")
        conn.execute("UPDATE files SET expires_at = 0 WHERE uploaded_by = 'worker050@box'")
        conn.execute("UPDATE tickets SET done_at = ? WHERE id = 1", (time.time() + 1,))
    page = browser.get(
        "/stats/data",
        params={
            "tickets_before": first["pages"]["tickets"]["next"],
            "files_after": first["pages"]["files"]["next"],
        },
    ).json()
    assert page["pages"]["tickets"]["total"] == page["pages"]["files"]["total"] == 104
    assert [item["ticket_id"] for item in page["ticket_cycle_time_days"]] == list(range(55, 5, -1))
    assert page["files_live_bytes_per_uploader"][0]["uploader"] == "worker054@box"
    assert all(row["uploader"] != "worker050@box" for row in page["files_live_bytes_per_uploader"])


def test_stats_page_queries_use_limits_and_ticket_index(
    browser: TestClient, settings: Settings, human_password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from board import db

    _log_in(browser, human_password)
    _seed_paged_stats(settings)
    statements: list[str] = []
    original = db.connect

    def traced(path: Path) -> sqlite3.Connection:
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db, "connect", traced)
    assert browser.get("/stats/data").status_code == 200
    assert sum("LIMIT 50" in sql for sql in statements) == 3
    assert not any("FROM profiles" in sql for sql in statements)
    with closing(sqlite3.connect(settings.db_path)) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM tickets WHERE done_at >= ? ORDER BY done_at DESC, id DESC LIMIT 50",
            (0,),
        ).fetchall()
        assert any("tickets_done_id" in row[3] for row in plan)
        assert not any("TEMP B-TREE" in row[3] for row in plan)


def test_stats_post_principal_pages_exclude_discarded_future_days(
    browser: TestClient, settings: Settings, human_password: str
) -> None:
    _log_in(browser, human_password)
    _seed_paged_stats(settings, 1)
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO posts (thread_id, author, author_kind, body, created_at) "
            "VALUES (1, 'future@box', 'agent', 'Future', ?)",
            (time.time() + 100 * DAY_SECONDS,),
        )
    data = browser.get("/stats/data").json()
    assert data["pages"]["posts"]["total"] == data["pages"]["posts"]["shown"] == 1
    assert len(data["posts_per_principal_per_day"]) == WINDOW_DAYS
    assert all("future@box" not in counts for counts in data["posts_per_principal_per_day"].values())


def test_stats_ranked_pages_keep_global_leaders_first(
    browser: TestClient, settings: Settings, human_password: str
) -> None:
    _log_in(browser, human_password)
    _seed_paged_stats(settings)
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.executemany(
            "INSERT INTO posts (thread_id, author, author_kind, body, created_at) "
            "VALUES (1, 'worker104@box', 'agent', 'Extra', ?)",
            [(time.time(),)] * 5,
        )
    data = browser.get("/stats/data").json()
    assert data["files_live_bytes_per_uploader"][0]["uploader"] == "worker104@box"
    counts = {
        name: count for day in data["posts_per_principal_per_day"].values() for name, count in day.items()
    }
    assert counts["worker104@box"] == 6 and "worker049@box" not in counts
    next_page = browser.get("/stats/data", params={"posts_after": data["pages"]["posts"]["next"]}).json()
    assert all("worker104@box" not in day for day in next_page["posts_per_principal_per_day"].values())


def test_stats_ranked_file_ties_reconstruct_without_gaps(
    browser: TestClient, settings: Settings, human_password: str
) -> None:
    _log_in(browser, human_password)
    _seed_paged_stats(settings)
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE files SET size = 10")
    params: dict[str, str] = {}
    seen: list[str] = []
    for count in (50, 50, 5):
        data = browser.get("/stats/data", params=params).json()
        assert len(data["files_live_bytes_per_uploader"]) == count
        seen.extend(row["uploader"] for row in data["files_live_bytes_per_uploader"])
        params = {"files_after": data["pages"]["files"]["next"]}
    assert seen == [f"worker{i:03}@box" for i in range(105)]
    previous = browser.get("/stats/data", params={"files_before": data["pages"]["files"]["previous"]}).json()
    assert previous["files_live_bytes_per_uploader"][0]["uploader"] == "worker050@box"


@pytest.mark.parametrize("metric", ["posts", "files"])
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [True, "human"],
        [-1, "human"],
        [float("nan"), "human"],
        [2**63, "human"],
        [1, None],
        [1, ""],
        ["1", "human"],
    ],
)
def test_stats_rank_cursors_are_strict(
    browser: TestClient, human_password: str, metric: str, payload: object
) -> None:
    import json

    _log_in(browser, human_password)
    token = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    assert browser.get("/stats/data", params={f"{metric}_after": token}).status_code == 400


def test_stats_chart_data_protects_and_validates_cursors(browser: TestClient, human_password: str) -> None:
    assert browser.get("/stats/data", follow_redirects=False).status_code == 303
    assert browser.get("/stats/data", headers={"HX-Request": "true"}).status_code == 401
    _log_in(browser, human_password)
    empty = browser.get("/stats/data").json()
    assert all(meta["total"] == meta["shown"] == 0 for meta in empty["pages"].values())
    for params in (
        {"posts_before": "a", "posts_after": "b"},
        {"tickets_before": "invalid"},
        {"files_before": "a", "files_after": "b"},
    ):
        assert browser.get("/stats/data", params=params).status_code == 400


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
