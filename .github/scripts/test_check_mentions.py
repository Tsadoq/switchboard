import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from check_mentions import mentions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please ping @human about it", ["human"]),
        ("ping @Human and @HUMAN", ["Human", "HUMAN"]),
        ("ask @pi@dev and @claude", ["pi", "claude"]),
        ("@all wakes everyone", ["all"]),
        ("call @matteo", ["matteo"]),
        ("cc @Tsadoq and @tsadoq", []),
        ("mail a@example.com, path/@scope/pkg, x@y", []),
        ("`@human` in a closed span", []),
        ("``code with ` and @human``", []),
        ("```\n@human in a fence\n```\nafter", []),
        ("~~~python\n@human\n~~~", []),
        ("lone `@human with no closing tick", ["human"]),
        ("escaped \\`@human\\` is not code", ["human"]),
        ("escaped \\`@human with a later ` tick", ["human"]),
        ("```\nan unclosed fence @human", ["human"]),
        ("` spans do not cross\nlines @human `", ["human"]),
    ],
)
def test_markdown_bodies(text: str, expected: list[str]) -> None:
    assert mentions(text, markdown=True) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Limit `@human` mentions", ["human"]),
        ("```\n@human\n```", ["human"]),
        ("Thanks @Tsadoq", []),
        ("Batch the human's pushes", []),
    ],
)
def test_titles_and_commit_messages_are_plain_text(text: str, expected: list[str]) -> None:
    assert mentions(text, markdown=False) == expected
