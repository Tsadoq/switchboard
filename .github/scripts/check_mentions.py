import re
import sys

ALLOWED = {"tsadoq"}
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MENTION = re.compile(r"(?<![\w`/.@-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")


def mentions(text: str) -> list[str]:
    prose = _INLINE_CODE.sub("", _FENCE.sub("", text))
    return [handle for handle in _MENTION.findall(prose) if handle.lower() not in ALLOWED]


def main() -> int:
    found = mentions(sys.stdin.read())
    if not found:
        return 0
    handles = ", ".join(sorted({f"@{handle}" for handle in found}))
    print(
        f"GitHub would notify {handles}. Board handles such as @human, @all, @claude and @pi are real "
        "GitHub users. Write 'the human' or the agent's name without @, put code in backticks, "
        "and mention only @Tsadoq when the owner must be notified.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
