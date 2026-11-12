import re
from collections.abc import Iterable
from collections.abc import Set as AbstractSet

from markdown_it import MarkdownIt

from board.constants import HUMAN

EVERYONE = "all"
_LABEL = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
# The lookahead keeps a name whole: `@claude@*` and `@claude…` are patterns or elisions, not
# the kind mention `@claude`, and a third `@` makes the whole token mention nobody.
MENTION = re.compile(rf"(?<![\w.@-])@({_LABEL}(?:@{_LABEL})?)(?![\w@*…-])")


_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough").disable("image")


def _prose(body: str) -> str:
    """The body's text with code spans and code blocks removed, as the web view renders them."""
    parts: list[str] = []
    for block in _MARKDOWN.parse(body):
        if block.type != "inline":
            continue
        for child in block.children or []:
            parts.append(child.content if child.type == "text" else " ")
        parts.append("\n")
    return "".join(parts)


def extract(body: str) -> set[str]:
    return set(MENTION.findall(_prose(body)))


def resolve(tokens: Iterable[str], known_principals: AbstractSet[str]) -> set[str]:
    resolved: set[str] = set()
    for token in tokens:
        if token == EVERYONE:
            resolved |= known_principals
        elif token == HUMAN:
            resolved.add(HUMAN)
        elif "@" in token:
            resolved |= {token} & known_principals
        else:
            resolved |= {principal for principal in known_principals if principal.partition("@")[0] == token}
    return resolved


def format(principal: str) -> str:
    return f"@{principal}"
