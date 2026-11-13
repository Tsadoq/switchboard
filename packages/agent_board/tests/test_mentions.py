import argparse
import subprocess
import sys

import pytest
from agent_board import mentions
from agent_board.commands import CommandError, forum
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


@pytest.mark.parametrize(
    ("body", "broadcast", "refused"),
    [
        pytest.param("cc @all", False, True, id="bare"),
        pytest.param("the handle `@all`", False, True, id="code-span-cannot-be-told-apart"),
        pytest.param("the handle `@all`", True, False, id="broadcast"),
        pytest.param("@claude@testbox, please look", False, False, id="no-handle"),
    ],
)
def test_without_markdown_it_any_all_agents_handle_is_refused_and_the_refusal_says_why(
    monkeypatch: pytest.MonkeyPatch, body: str, broadcast: bool, refused: bool
) -> None:
    monkeypatch.setattr(mentions, "MARKDOWN", None)
    args = argparse.Namespace(broadcast=broadcast)

    if not refused:
        assert forum.checked_body(args, body) == body
        return
    with pytest.raises(CommandError, match="lacks markdown-it-py.*--broadcast"):
        forum.checked_body(args, body)


@pytest.mark.parametrize("module", ["agent_board.night.__main__", "agent_board.cli"])
def test_the_package_imports_where_markdown_it_is_not_installed(module: str) -> None:
    # `sys.modules[name] = None` makes the import fail, as it does in the night verifier's install.
    probe = (
        "import importlib, sys\n"
        "sys.modules['markdown_it'] = None\n"
        f"importlib.import_module({module!r})\n"
        "from agent_board import mentions\n"
        "assert mentions.MARKDOWN is None, 'the parser loaded although markdown_it cannot be imported'\n"
    )

    ran = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=False
    )

    assert ran.returncode == 0, f"{module} needs markdown-it-py: {ran.stderr}"


def test_the_night_verifier_never_loads_the_markdown_parser() -> None:
    probe = (
        "import importlib, sys\n"
        "importlib.import_module('agent_board.night.__main__')\n"
        "loaded = sorted(name for name in sys.modules if name.split('.')[0] in ('markdown_it', 'mdurl'))\n"
        "assert not loaded, f'the night entrypoint loaded {loaded}'\n"
    )

    ran = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=False
    )

    assert ran.returncode == 0, f"the night entrypoint imports the CLI-only parser: {ran.stderr}"
