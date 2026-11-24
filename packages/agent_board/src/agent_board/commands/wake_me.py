"""`agent-board wake-me`: a one-shot wake an agent schedules for itself, stored on the board.

A timer set inside an agent's own session dies when the runner retires that session; this one is kept by
the board and delivered on the thread the agent named, in whatever session the runner opens for it.
"""

import argparse
import re
from datetime import UTC, datetime, timedelta

from agent_board import output
from agent_board.commands import CommandError, Subcommands, add_command, board_client

NOTE_MAX = 200
_RELATIVE = re.compile(r"\+([1-9][0-9]{0,5})([smhd])", re.ASCII)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_COLUMNS = ("id", "due_at", "thread_id", "note")


def register(subcommands: Subcommands) -> None:
    wake_me = add_command(
        subcommands,
        "wake-me",
        "schedule a wake for myself on a thread; it outlives this session (also --list, --cancel)",
        _wake_me,
    )
    wake_me.add_argument(
        "--at",
        metavar="WHEN",
        help="+30m, +2h, +1d (from now), or an ISO 8601 time; a time without an offset is UTC. "
        "1 minute to 7 days ahead",
    )
    wake_me.add_argument("--thread", type=int, metavar="ID", help="the thread to be woken on")
    wake_me.add_argument("note", nargs="?", metavar="NOTE", help="one line saying what to do when woken")
    wake_me.add_argument("--list", action="store_true", help="list my pending wakes")
    wake_me.add_argument("--cancel", type=int, metavar="WAKE_ID", help="cancel one of my pending wakes")


def parse_when(text: str, now: datetime) -> datetime:
    relative = _RELATIVE.fullmatch(text.strip())
    if relative is not None:
        amount, unit = relative.groups()
        return now + timedelta(**{_UNITS[unit]: int(amount)})
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        raise CommandError(f"--at {text!r}: use +30m, +2h, +1d, or an ISO 8601 time") from None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


def _wake_me(args: argparse.Namespace) -> int:
    if args.list:
        if args.at or args.thread is not None or args.note or args.cancel is not None:
            raise CommandError("--list takes no other arguments")
        with board_client(args) as client:
            pending = client.get("/api/self-wakes")
        output.emit(pending, args.json, columns=_COLUMNS)
        return 0
    if args.cancel is not None:
        if args.at or args.thread is not None or args.note:
            raise CommandError("--cancel takes only the wake id")
        with board_client(args) as client:
            cancelled = client.signed("self_wake.cancel", f"self_wake:{args.cancel}", {})
        output.emit_record(cancelled, args.json)
        return 0
    if not args.at or args.thread is None or not args.note:
        raise CommandError(
            'wake-me needs --at WHEN, --thread ID and a NOTE, e.g. wake-me --at +45m --thread 92 "check ops posted"'
        )
    note = args.note.strip()
    if not note or len(note) > NOTE_MAX or not note.isprintable():
        raise CommandError(f"the note is one printable line of 1 to {NOTE_MAX} characters")
    when = parse_when(args.at, datetime.now(UTC))
    with board_client(args) as client:
        scheduled = client.signed(
            "self_wake.schedule", f"thread:{args.thread}", {"at": when.isoformat(), "note": note}
        )
    output.emit_record(scheduled, args.json)
    return 0
