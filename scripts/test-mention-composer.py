import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packages/board/src/board/web/static/mentions.js"


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM_EXECUTABLE"))
        page = browser.new_page()
        page.route(
            "https://board.test/",
            lambda route: route.fulfill(content_type="text/html", body='<textarea name="body"></textarea>'),
        )
        page.route(
            "**/mention-options?**",
            lambda route: route.fulfill(
                content_type="application/json",
                body=json.dumps([{"value": "@pi@dev"}, {"value": "@pi@huginn"}]),
            ),
        )
        page.goto("https://board.test/")
        page.add_script_tag(path=str(SCRIPT))
        field = page.locator("textarea")
        field.fill("Review @pi")
        page.locator('[role="option"]').first.wait_for()
        field.press("ArrowDown")
        field.press("Enter")
        assert field.input_value() == "Review @pi@huginn "
        assert field.get_attribute("aria-expanded") == "false"

        field.fill("Ask @pi")
        page.locator('[role="option"]').first.wait_for()
        page.locator('[role="option"]').first.click()
        assert field.input_value() == "Ask @pi@dev "
        assert field.evaluate("element => element === document.activeElement")

        field.fill("Ask @pi")
        page.locator('[role="option"]').first.wait_for()
        field.press("Escape")
        assert field.get_attribute("aria-expanded") == "false"
        assert field.input_value() == "Ask @pi"

        field.fill("Ask @pi")
        page.locator('[role="option"]').first.wait_for()
        field.press("Tab")
        assert field.input_value() == "Ask @pi@dev "

        field.fill("mail@example.org")
        assert field.get_attribute("aria-expanded") == "false"
        field.fill("No mention")
        field.press("Enter")
        assert "\n" in field.input_value()
        browser.close()
    print("Mention composer browser checks passed")


if __name__ == "__main__":
    main()
