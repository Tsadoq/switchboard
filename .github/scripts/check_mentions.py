import re
import sys

ALLOWED = {"tsadoq"}
# Code spans and fences are not exempt: the owner asked for no board handles on GitHub at all,
# and every Markdown exception tried so far had a bypass.
_MENTION = re.compile(r"(?<![A-Za-z0-9_/.@-])@([A-Za-z0-9][A-Za-z0-9-]{0,38})")


def mentions(text: str) -> list[str]:
    return [handle for handle in _MENTION.findall(text) if handle.lower() not in ALLOWED]


def main() -> int:
    found = mentions(sys.stdin.read())
    if not found:
        return 0
    handles = ", ".join(sorted({f"@{handle}" for handle in found}))
    print(
        f"GitHub would notify {handles}, or the text names a handle with an at-sign. Board handles "
        "such as human, all, claude and pi are real GitHub users. Write 'the human' or the agent's "
        "name without the at-sign, also inside backticks; only @Tsadoq is allowed.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
