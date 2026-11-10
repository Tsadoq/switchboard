import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from board import passwords
from board.app import create_app
from board.settings import Settings
from fastapi.testclient import TestClient
from playwright.sync_api import Route, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with TemporaryDirectory() as directory:
        settings = Settings.from_env(
            {
                "BOARD_STATE_DIR": directory,
                "BOARD_PUBLIC_URL": "https://board.test",
                "BOARD_HUMAN_PASSWORD_HASH": passwords.hash_password("test-only"),
                "BOARD_HUMAN_DIGEST_SECONDS": "0",
            }
        )
        with TestClient(create_app(settings), base_url="https://board.test") as client:
            client.post("/login", data={"password": "test-only"})
            client.post("/f/general/threads", data={"title": "Pagination browser test", "body": "first"})
            with closing(sqlite3.connect(settings.db_path)) as conn, conn:
                conn.executemany(
                    "INSERT INTO posts (thread_id, author, author_kind, body, created_at) "
                    "VALUES (1, 'human', 'human', ?, ?)",
                    [(f"post {i}", i) for i in range(2, 81)],
                )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM_EXECUTABLE"))
                page = browser.new_page()
                polls: list[str] = []
                fail_next = False
                close_next = False

                def serve(route: Route) -> None:
                    nonlocal fail_next
                    url = urlsplit(route.request.url)
                    if url.netloc != "board.test" or route.request.method != "GET":
                        route.abort()
                        return
                    if url.path.endswith("/posts"):
                        polls.append(url.query)
                        if fail_next:
                            fail_next = False
                            route.fulfill(status=503, body="Temporary error")
                            return
                    response = client.get(
                        url.path + (f"?{url.query}" if url.query else ""),
                        headers={"HX-Request": "true"} if url.path.endswith("/posts") else {},
                        follow_redirects=False,
                    )
                    headers = {
                        k: v
                        for k, v in response.headers.items()
                        if k not in {"content-length", "content-encoding"}
                    }
                    status = response.status_code
                    if status == 303:
                        route.fulfill(
                            content_type="text/html",
                            body=f"<script>location.replace({json.dumps(headers['location'])})</script>",
                        )
                        return
                    if close_next and url.path.endswith("/posts") and headers.get("x-has-more") == "false":
                        status = 286
                    route.fulfill(status=status, headers=headers, body=response.content)

                page.route("**/*", serve)
                page.clock.install()
                page.goto("https://board.test/t/1")
                assert page.locator("article.post").count() == 50
                assert page.locator("article.post").first.get_attribute("id") == "post-31"
                with closing(sqlite3.connect(settings.db_path)) as conn, conn:
                    conn.executemany(
                        "INSERT INTO posts (thread_id, author, author_kind, body, created_at) "
                        "VALUES (1, 'human', 'human', ?, ?)",
                        [(f"post {i}", i) for i in range(81, 201)],
                    )
                fail_next = True
                page.clock.run_for(10000)
                page.wait_for_function(
                    "document.querySelector('#poll-status').textContent.includes('Retrying')"
                )
                assert page.locator("#posts").get_attribute("data-after") == "80"
                close_next = True
                for _ in range(12):
                    page.clock.run_for(10000)
                    page.wait_for_timeout(30)
                    if page.locator("#posts").get_attribute("data-after") == "200":
                        break
                assert page.locator("#posts").get_attribute("data-after") == "200"
                assert polls == ["after=80", "after=80", "after=130", "after=180"]
                assert page.locator("article.post").count() == 50
                assert page.locator("article.post").first.get_attribute("id") == "post-151"
                assert page.locator("#older-posts").get_attribute("href") == "/t/1?before=151"
                assert "done" in page.locator("#poll-status").inner_text()
                count = len(polls)
                page.clock.run_for(60000)
                assert len(polls) == count

                close_next = False
                page.goto("https://board.test/t/1?before=51")
                count = len(polls)
                page.clock.run_for(60000)
                assert len(polls) == count
                assert page.locator("#posts").get_attribute("data-live") == "false"
                page.goto("https://board.test/t/1")
                page.evaluate("location.hash = 'post-2'")
                page.wait_for_url("**/t/1?before=3#post-2")
                assert page.locator("#post-2").count() == 1
                page.go_back()
                assert page.url == "https://board.test/t/1", page.url
                page.go_back()
                assert "before=51" in page.url, page.url
                page.goto("https://board.test/t/1")
                page.evaluate("location.hash = 'post-80'")
                page.wait_for_url("**/t/1?before=81#post-80")
                assert page.locator("#post-80").count() == 1
                assert page.locator("article.post").count() == 50
                assert page.locator("article.post").first.get_attribute("id") == "post-31"
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path="/tmp/switchboard-thread-pagination.png")
                browser.close()
    print(
        "Thread pagination browser checks passed: bounded DOM, retries, batch draining, stop, older pages and legacy history"
    )


if __name__ == "__main__":
    main()
