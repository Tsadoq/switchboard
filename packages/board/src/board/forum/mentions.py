import re
from collections.abc import Iterable
from collections.abc import Set as AbstractSet

from board.constants import HUMAN

EVERYONE = "all"
_LABEL = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
_MENTION = re.compile(rf"(?<![\w.@-])@({_LABEL}(?:@{_LABEL})?)")


def extract(body: str) -> set[str]:
    return set(_MENTION.findall(body))


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
