from datetime import UTC, datetime, timedelta, timezone

import pytest
from agent_board.commands import CommandError
from agent_board.commands.wake_me import parse_when

NOW = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+90s", NOW + timedelta(seconds=90)),
        ("+45m", NOW + timedelta(minutes=45)),
        ("+2h", NOW + timedelta(hours=2)),
        ("+1d", NOW + timedelta(days=1)),
        ("2026-11-25T07:40:00Z", datetime(2026, 11, 25, 7, 40, tzinfo=UTC)),
        ("2026-11-25T07:40", datetime(2026, 11, 25, 7, 40, tzinfo=UTC)),
        ("2026-11-25T08:40:00+01:00", datetime(2026, 11, 25, 8, 40, tzinfo=timezone(timedelta(hours=1)))),
    ],
)
def test_parse_when_reads_relative_and_absolute_times(text: str, expected: datetime) -> None:
    assert parse_when(text, NOW) == expected


@pytest.mark.parametrize("text", ["tomorrow", "+0m", "+5x", "45m", "+-5m", "", "+1234567h"])
def test_parse_when_refuses_what_it_cannot_read(text: str) -> None:
    with pytest.raises(CommandError):
        parse_when(text, NOW)
