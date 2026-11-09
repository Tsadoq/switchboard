import argparse
import re
import sys

ALLOWED = {"tsadoq"}
_FENCE = re.compile(r"^(?P<fence>```+|~~~+)[^\n]*\n.*?^(?P=fence)[ \t]*$", re.MULTILINE | re.DOTALL)
_ESCAPED_BACKTICK = re.compile(r"\\`")
_INLINE_CODE = re.compile(r"(?P<ticks>`+)(?!`)[^\n]*?(?<!`)(?P=ticks)(?!`)")
_MENTION = re.compile(r"(?<![\w/.@-])@([A-Za-z0-9][A-Za-z0-9-]{0,38})")


def mentions(text: str, *, markdown: bool) -> list[str]:
    if markdown:
        text = _FENCE.sub("", text)
        text = _INLINE_CODE.sub("", _ESCAPED_BACKTICK.sub("", text))
    return [handle for handle in _MENTION.findall(text) if handle.lower() not in ALLOWED]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail when text would notify a GitHub user.")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="treat the input as Markdown, where closed code spans and fenced blocks do not notify",
    )
    args = parser.parse_args()
    found = mentions(sys.stdin.read(), markdown=args.markdown)
    if not found:
        return 0
    handles = ", ".join(sorted({f"@{handle}" for handle in found}))
    print(
        f"GitHub would notify {handles}. Board handles such as human, all, claude and pi are real "
        "GitHub users. Write 'the human' or the agent's name without the at-sign. Titles and commit "
        "messages are plain text, so backticks do not protect a mention there.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
