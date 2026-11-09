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
        ("`@human` in a closed span", ["human"]),
        ("```\n@human in a fence\n```", ["human"]),
        ("``@human` with uneven ticks", ["human"]),
        ("word`code`@human", ["human"]),
        ("word\\`@human", ["human"]),
        ("lone `@human", ["human"]),
        ("(@human) and [@human]", ["human", "human"]),
        ("cc @Tsadoq and @tsadoq", []),
        ("mail a@example.com, path/@scope/pkg, x@y", []),
        ("Batch the human's pushes", []),
    ],
)
def test_any_handle_but_the_owner_fails_wherever_it_appears(text: str, expected: list[str]) -> None:
    assert mentions(text) == expected
