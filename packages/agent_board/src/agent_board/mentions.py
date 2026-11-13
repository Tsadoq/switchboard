"""Which handles a post body mentions, decided the way the board decides it.

This repeats `board.forum.mentions.extract` because the client does not depend on the server
package. `test_mentions.py` holds the two to the same answers, so a change there fails here.
"""

import re

from markdown_it import MarkdownIt

EVERYONE = "all"
_LABEL = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
MENTION = re.compile(rf"(?<![\w.@-])@({_LABEL}(?:@{_LABEL})?)(?![\w@*…-])")

_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough").disable("image")


def extract(body: str) -> set[str]:
    return set(MENTION.findall(_prose(body)))


def broadcasts(body: str) -> bool:
    """Whether posting this body would wake every registered agent."""
    return EVERYONE in extract(body)


def _prose(body: str) -> str:
    parts: list[str] = []
    for block in _MARKDOWN.parse(body):
        if block.type != "inline":
            continue
        for child in block.children or []:
            parts.append(child.content if child.type == "text" else " ")
        parts.append("\n")
    return "".join(parts)
