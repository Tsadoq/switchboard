import pytest
from board.web.rendering import render_post


def test_markdown_renders_structure_and_mentions() -> None:
    rendered = render_post("# Heading\n\n**bold** and @human @pi@dev @pi @all\n\n- one\n- two")
    assert "<h1>Heading</h1>" in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<ul>" in rendered
    assert 'class="mention" href="/mentions">@human</a>' in rendered
    assert 'class="mention mention-agent" href="/agents?mention=pi%40dev">@pi@dev</a>' in rendered
    assert 'class="mention mention-agent" href="/agents?mention=pi">@pi</a>' in rendered
    assert 'class="mention mention-agent" href="/agents">@all</a>' in rendered


@pytest.mark.parametrize(
    "source",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "[click](javascript:alert(1))",
        "[click](data:text/html,bad)",
        "[click](vbscript:bad)",
        "[click](jav&#x61;script:bad)",
    ],
)
def test_markdown_cannot_create_executable_markup(source: str) -> None:
    rendered = render_post(source)
    assert "<script" not in rendered
    assert "<img" not in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="data:' not in rendered
    assert 'href="vbscript:' not in rendered


def test_mentions_do_not_rewrite_code_links_or_email_addresses() -> None:
    rendered = render_post(
        "`@human`\n\n```text\n@pi@dev\n```\n\n[@human](https://example.org)\n\nmail@example.org"
    )
    assert 'class="mention"' not in rendered
    assert "<code>@human</code>" in rendered
    assert '<a href="https://example.org">@human</a>' in rendered
    assert "mail@example.org" in rendered


def test_tables_and_quotes_and_code_remain_readable() -> None:
    rendered = render_post("| A | B |\n|---|---|\n| 1 | 2 |\n\n> quote\n\n```html\n<script>\n```")
    assert "<table>" in rendered
    assert "<blockquote>" in rendered
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered


def test_plain_post_line_breaks_are_preserved() -> None:
    rendered = render_post("Status:\nline one\nline two")
    assert "Status:<br />\nline one<br />\nline two" in rendered


def test_external_images_are_not_embedded() -> None:
    assert "<img" not in render_post("![tracking image](https://example.org/track)")
