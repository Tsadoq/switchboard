from datetime import UTC, datetime
from pathlib import Path

import jinja2
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from board.web.rendering import render_post

_TEMPLATES_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")
_DURATION_UNITS = (("w", 7 * 86400), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def _when(epoch_seconds: float | None) -> str:
    if epoch_seconds is None:
        return "never"
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _duration(seconds: int) -> str:
    parts = []
    for unit, unit_seconds in _DURATION_UNITS:
        count, seconds = divmod(seconds, unit_seconds)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts) or "0s"


def _updates_waiting(request: Request) -> int:
    updates = getattr(request.app.state, "updates", None)
    return updates.waiting() if updates is not None else 0


templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES_DIR), autoescape=True, undefined=jinja2.StrictUndefined
    )
)
templates.env.filters.update(when=_when, duration=_duration, markdown=render_post)
templates.env.globals.update(updates_waiting=_updates_waiting)
