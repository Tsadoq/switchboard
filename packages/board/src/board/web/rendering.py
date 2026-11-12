from html import escape
from html.parser import HTMLParser
from urllib.parse import quote

from markdown_it import MarkdownIt
from markupsafe import Markup

from board.forum.mentions import MENTION

_MARKDOWN = (
    MarkdownIt("commonmark", {"html": False, "breaks": True})
    .enable("table")
    .enable("strikethrough")
    .disable("image")
)


def mention_url(mention: str) -> str:
    if mention == "human":
        return "/mentions"
    if mention == "all":
        return "/agents"
    return f"/agents?mention={quote(mention, safe='')}"


class _MentionLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.protected = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"a", "code", "pre"}:
            self.protected += 1
        self.parts.append(self.get_starttag_text() or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"a", "code", "pre"}:
            self.protected -= 1
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.protected:
            self.parts.append(escape(data))
            return
        start = 0
        for match in MENTION.finditer(data):
            self.parts.append(escape(data[start : match.start()]))
            href = escape(mention_url(match[1]), quote=True)
            classes = "mention" if match[1] == "human" else "mention mention-agent"
            self.parts.append(f'<a class="{classes}" href="{href}">{escape(match[0])}</a>')
            start = match.end()
        self.parts.append(escape(data[start:]))


def render_post(body: str) -> Markup:
    parser = _MentionLinks()
    parser.feed(_MARKDOWN.render(body))
    parser.close()
    return Markup("".join(parser.parts))
