from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from agent_board.config import ConfigError
from agent_board.runner.loop import model_for
from agent_board.runner.weekly import WeeklyClock, WeeklySlot

ROME = ZoneInfo("Europe/Rome")
MONDAY_SIX = WeeklySlot.parse("Mon 06:00 Europe/Rome", "")


def _at(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=ROME).timestamp()


@pytest.mark.parametrize(
    "text", ["Monday 06:00 Europe/Rome", "Mon 6:00 Europe/Rome", "Mon 24:00 UTC", "Mon 06:00 Mars/Base"]
)
def test_bad_weekly_slots_are_config_errors(text: str) -> None:
    with pytest.raises(ConfigError):
        WeeklySlot.parse(text, "kind.pi.")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_at(2026, 11, 9, 6, 0), _at(2026, 11, 9, 6, 0)),
        (_at(2026, 11, 9, 5, 59), _at(2026, 11, 2, 6, 0)),
        (_at(2026, 11, 15, 23, 0), _at(2026, 11, 9, 6, 0)),
        (_at(2026, 3, 30, 7, 0), _at(2026, 3, 30, 6, 0)),
        (_at(2026, 10, 26, 7, 0), _at(2026, 10, 26, 6, 0)),
    ],
)
def test_latest_slot_is_local_wall_clock_across_dst(now: float, expected: float) -> None:
    assert MONDAY_SIX.latest_at_or_before(now) == expected


def test_a_new_clock_records_now_instead_of_firing(tmp_path: Path) -> None:
    clock = WeeklyClock(MONDAY_SIX, tmp_path / "weekly.json")

    assert clock.due(_at(2026, 11, 9, 12, 0)) is False
    assert clock.due(_at(2026, 11, 15, 12, 0)) is False
    assert clock.due(_at(2026, 11, 16, 6, 0)) is True


def test_a_missed_run_of_weeks_fires_once_and_survives_restart(tmp_path: Path) -> None:
    state = tmp_path / "weekly.json"
    WeeklyClock(MONDAY_SIX, state).mark(_at(2026, 10, 1, 0, 0))
    now = _at(2026, 11, 11, 9, 0)

    assert WeeklyClock(MONDAY_SIX, state).due(now) is True
    WeeklyClock(MONDAY_SIX, state).mark(now)
    assert WeeklyClock(MONDAY_SIX, state).due(now + 3600) is False


MODELS = {"default": "d", "mention": "big", "timer": "small", "weekly": "weekly"}


@pytest.mark.parametrize(
    ("reasons", "models", "expected"),
    [
        (["timer"], MODELS, "small"),
        (["timer", "mention"], MODELS, "big"),
        (["follow"], MODELS, "d"),
        (["weekly", "mention"], MODELS, "weekly"),
        (["mention"], {}, None),
        (["timer"], {"mention": "big"}, None),
    ],
)
def test_model_for_picks_the_highest_priority_reason(
    reasons: list[str], models: dict[str, str], expected: str | None
) -> None:
    assert model_for([{"reason": reason} for reason in reasons], models) == expected
