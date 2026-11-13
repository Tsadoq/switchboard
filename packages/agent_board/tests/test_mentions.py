import pytest
from agent_board import mentions
from board.forum import mentions as board_mentions

# Bodies where a rule that only looked for the text would get the answer wrong, one way or the other.
_BODIES = [
    "hello @all",
    "(@pi), @human's turn; @all!",
    '> he wrote "@all please read"',
    "'@all' in plain quotes still wakes everyone",
    "see `@human`, `@all`, `@claude@testbox` and `@pi@testbox`",
    "``@pi `tick` @all``",
    "example:\n\n~~~text\n@all\n~~~\n",
    "example:\n\n    @all indented\n",
    "- item\n\n  ```\n  @all\n  ```\n",
    "```\n@all in a fence the author never closed",
    "```\n@pi\n```\n\n@all see above",
    "ask `@all\nand @pi@testbox` later",
    "ask `@all\n\nand @pi@testbox` later",
    "a stray ` backtick, then @all",
    "`code`@all",
    "**@all** and _@human_",
    "| a |\n|---|\n| @all |",
    "see [@all](https://example.org)",
    "<code>@all</code>",
    "mail all@example.org or ops@all.example.org",
    "handles like @all… and @all* and @all@*",
    "@all@testbox and @claude@all",
    "@allof you, @all-hands, @all.",
    "@ALL and @All",
    "no handle here",
]


def test_the_client_reads_mentions_with_the_boards_own_pattern_and_markdown_rules() -> None:
    assert mentions.EVERYONE == board_mentions.EVERYONE, "the all-agents handle differs from the board's"
    assert mentions.MENTION.pattern == board_mentions.MENTION.pattern, "the mention pattern drifted"
    assert mentions.MENTION.flags == board_mentions.MENTION.flags, "the mention pattern's flags drifted"


@pytest.mark.parametrize("body", _BODIES)
def test_the_client_extracts_what_the_board_extracts(body: str) -> None:
    extracted = mentions.extract(body)

    assert extracted == board_mentions.extract(body), f"{body!r} extracted as {extracted} by the client"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param("hello @all", True, id="bare"),
        pytest.param('he wrote "@all please read"', True, id="plain-quotes"),
        pytest.param("> @all", True, id="blockquote"),
        pytest.param("the handle `@all` wakes every agent", False, id="code-span"),
        pytest.param("```\n@all\n```\n", False, id="fenced-block"),
        pytest.param("@claude@testbox and @human, please look", False, id="other-mentions"),
        pytest.param("@all@testbox is a principal, @ALL is not the handle", False, id="near-misses"),
    ],
)
def test_broadcasts_only_when_the_all_agents_handle_is_a_real_mention(body: str, expected: bool) -> None:
    assert mentions.broadcasts(body) is expected, f"{body!r} broadcast={mentions.broadcasts(body)}"
