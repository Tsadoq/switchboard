import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_board.config import ConfigError

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SLOT = re.compile(r"(?P<day>[A-Za-z]{3}) (?P<hour>[01][0-9]|2[0-3]):(?P<minute>[0-5][0-9]) (?P<zone>\S+)")


@dataclass(frozen=True)
class WeeklySlot:
    weekday: int
    hour: int
    minute: int
    zone: ZoneInfo

    @classmethod
    def parse(cls, text: str, where: str) -> Self:
        found = _SLOT.fullmatch(text.strip())
        if found is None or found["day"].lower() not in _DAYS:
            raise ConfigError(
                f"runner config: {where}weekly must look like 'Mon 06:00 Europe/Rome', got {text!r}"
            )
        try:
            zone = ZoneInfo(found["zone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(
                f"runner config: {where}weekly names an unknown time zone {found['zone']!r}"
            ) from None
        return cls(_DAYS.index(found["day"].lower()), int(found["hour"]), int(found["minute"]), zone)

    def latest_at_or_before(self, now: float) -> float:
        local = datetime.fromtimestamp(now, self.zone)
        day = local.date() - timedelta(days=(local.weekday() - self.weekday) % 7)
        slot = datetime(day.year, day.month, day.day, self.hour, self.minute, tzinfo=self.zone)
        if slot.timestamp() > now:
            day -= timedelta(days=7)
            slot = datetime(day.year, day.month, day.day, self.hour, self.minute, tzinfo=self.zone)
        return slot.timestamp()


class WeeklyClock:
    def __init__(self, slot: WeeklySlot, state_file: Path) -> None:
        self._slot = slot
        self._state_file = state_file

    def due(self, now: float) -> bool:
        fired = self._last_fired()
        if fired is None:
            self.mark(now)
            return False
        return fired < self._slot.latest_at_or_before(now)

    def mark(self, now: float) -> None:
        self._state_file.write_text(json.dumps({"last_fired_at": now}))

    def _last_fired(self) -> float | None:
        try:
            value = json.loads(self._state_file.read_text())["last_fired_at"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return float(value) if isinstance(value, int | float) else None
