import os
import sqlite3
import time
from contextlib import closing
from tempfile import TemporaryDirectory
from uuid import UUID

from board import passwords
from board.app import create_app
from board.settings import Settings
from fastapi.testclient import TestClient
from playwright.sync_api import Route, sync_playwright


def main() -> None:
    with TemporaryDirectory() as directory:
        settings = Settings.from_env(
            {
                "BOARD_STATE_DIR": directory,
                "BOARD_PUBLIC_URL": "https://board.test",
                "BOARD_HUMAN_PASSWORD_HASH": passwords.hash_password("test-only"),
            }
        )
        with TestClient(create_app(settings), base_url="https://board.test") as client:
            client.post("/login", data={"password": "test-only"})
            now = time.time()
            with closing(sqlite3.connect(settings.db_path)) as conn, conn:
                for i in range(105):
                    principal = f"worker{i:03}@box"
                    thread_id = conn.execute(
                        "INSERT INTO threads (subforum, title, kind, created_by, created_at) "
                        "VALUES ('general', ?, 'ticket', 'human', ?)",
                        (f"Ticket {i:03}", now - 86400),
                    ).lastrowid
                    conn.execute(
                        "INSERT INTO posts (thread_id, author, author_kind, body, created_at) "
                        "VALUES (?, ?, 'agent', 'Post', ?)",
                        (thread_id, principal, now),
                    )
                    conn.execute(
                        'INSERT INTO tickets (title, "column", thread_id, created_by, created_at, updated_at, done_at) '
                        "VALUES (?, 'done', ?, 'human', ?, ?, ?)",
                        (f"Ticket {i:03}", thread_id, now - 86400, now, now),
                    )
                    conn.execute(
                        "INSERT INTO files (id, name, content_type, size, sha256, uploaded_by, created_at, expires_at) "
                        "VALUES (?, 'Sample', 'text/plain', ?, ?, ?, ?, ?)",
                        (str(UUID(int=i + 1)), i + 1, "0" * 64, principal, now, now + 3600),
                    )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM_EXECUTABLE"))
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def respond(route: Route) -> None:
                    response = client.get(route.request.url, headers={"HX-Request": "true"})
                    route.fulfill(
                        status=response.status_code, headers=dict(response.headers), body=response.content
                    )

                page.route("https://board.test/**", respond)
                page.goto("https://board.test/stats")
                tickets = page.locator('figure[data-chart="ticket_cycle_time_days"]')
                files = page.locator('figure[data-chart="files_live_bytes_per_uploader"]')
                tickets.get_by_role("link", name="Next", exact=True).wait_for()
                assert tickets.locator("tbody tr").count() == 50
                assert "#105" in (tickets.locator("tbody tr").first.text_content() or "")
                tickets.get_by_role("link", name="Next", exact=True).click()
                tickets.get_by_role("link", name="Previous", exact=True).wait_for()
                assert "#55" in (tickets.locator("tbody tr").first.text_content() or "")
                files.get_by_role("link", name="Next", exact=True).click()
                files.get_by_role("link", name="Previous", exact=True).wait_for()
                assert "worker050@box" in (files.locator("tbody tr").first.text_content() or "")
                assert "#55" in (tickets.locator("tbody tr").first.text_content() or "")
                tickets.get_by_role("link", name="Previous", exact=True).click()
                tickets.get_by_role("link", name="Next", exact=True).wait_for()
                assert "files_after=" in page.url and "#105" in (
                    tickets.locator("tbody tr").first.text_content() or ""
                )
                assert "worker050@box" in (files.locator("tbody tr").first.text_content() or "")
                tickets.locator("summary").click()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                tickets.locator("summary").click()
                tickets.scroll_into_view_if_needed()
                page.screenshot(path="/tmp/switchboard-stats-pagination.png")
                assert not errors, errors
                assert page.locator("#stats-status").is_hidden()
                page.goto("https://board.test/stats?tickets_before=invalid")
                page.locator("#stats-status").wait_for(state="visible")
                page.get_by_role("link", name="Latest statistics (reset all chart pages)", exact=True).click()
                tickets.get_by_role("link", name="Next", exact=True).wait_for()
                assert page.locator("#stats-status").is_hidden()
                browser.close()
    print(
        "Stats pagination browser checks passed: bounds, next/previous, independent cursors and mobile table overflow"
    )


if __name__ == "__main__":
    main()
