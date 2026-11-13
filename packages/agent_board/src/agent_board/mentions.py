"""Which handles a post body mentions, decided the way the board decides it.

This repeats `board.forum.mentions.extract` because the client does not depend on the server
package. `test_mentions.py` holds the two to the same answers, so a change there fails here.

markdown-it-py comes with the `cli` extra, which the installer asks for. The night verifier installs
the package without it, so `MARKDOWN` is None there and only the callers that need it must check.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

EVERYONE = "all"
_LABEL = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
MENTION = re.compile(rf"(?<![\w.@-])@({_LABEL}(?:@{_LABEL})?)(?![\w@*…-])")


def _markdown() -> "MarkdownIt | None":
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return None
    return MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough").disable("image")


MARKDOWN = _markdown()


def extract(body: str) -> set[str]:
    return set(MENTION.findall(_prose(body)))


def broadcasts(body: str) -> bool:
    """Whether posting this body would wake every registered agent."""
    return EVERYONE in extract(body)


def _prose(body: str) -> str:
    if MARKDOWN is None:
        raise RuntimeError("markdown-it-py is not installed; install agent-board with the cli extra")
    parts: list[str] = []
    for block in MARKDOWN.parse(body):
        if block.type != "inline":
            continue
        for child in block.children or []:
            parts.append(child.content if child.type == "text" else " ")
        parts.append("\n")
    return "".join(parts)
