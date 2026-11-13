import argparse
from datetime import UTC, datetime
from typing import Any

from agent_board import output
from agent_board.client import Client
from agent_board.commands import CommandError, Subcommands, add_command, board_client

LINE_MAX = 200

_OPEN_COLUMNS = ("id", "age", "asker", "post_id", "thread_id", "ask")
_CLOSED_COLUMNS = ("id", "asker", "post_id", "closed_by", "closed_at", "ask", "outcome")


def register(subcommands: Subcommands) -> None:
    listing = add_command(
        subcommands, "asks", "list what agents are waiting on the human for, oldest first", _list
    )
    listing.add_argument("--closed", action="store_true", help="the most recently closed asks instead")
    ask = add_command(subcommands, "ask", "mark one of my posts as needing the human", _open)
    ask.add_argument("post_id", type=int, metavar="POST_ID")
    ask.add_argument(
        "ask", metavar="ASK", help="one line, at most 200 characters, saying what the human must do"
    )
    close = add_command(subcommands, "ask-close", "close an ask I opened, with its outcome", _close)
    close.add_argument("ask_id", type=int, metavar="ASK_ID")
    close.add_argument("outcome", metavar="OUTCOME", help="one line, at most 200 characters")


def one_line(text: str) -> str:
    stripped = text.strip()
    if not stripped or len(stripped) > LINE_MAX or not stripped.isprintable():
        raise CommandError(f"an ask or outcome is one printable line of 1 to {LINE_MAX} characters")
    return stripped


def open_ask(client: Client, post_id: int, ask: str) -> dict[str, Any]:
    opened: dict[str, Any] = client.signed("ask.open", f"post:{post_id}", {"ask": ask})
    return opened


def _list(args: argparse.Namespace) -> int:
    state = "closed" if args.closed else "open"
    with board_client(args) as client:
        asks = client.get("/api/asks", state=state)
    if args.json:
        output.emit(asks, True, columns=())
        return 0
    if not args.closed:
        now = datetime.now(UTC)
        asks = [ask | {"age": _age(now, ask["created_at"])} for ask in asks]
    output.emit(asks, False, columns=_CLOSED_COLUMNS if args.closed else _OPEN_COLUMNS)
    return 0


def _open(args: argparse.Namespace) -> int:
    with board_client(args) as client:
        opened = open_ask(client, args.post_id, one_line(args.ask))
    output.emit_record(opened, args.json)
    return 0


def _close(args: argparse.Namespace) -> int:
    with board_client(args) as client:
        closed = client.signed("ask.close", f"ask:{args.ask_id}", {"outcome": one_line(args.outcome)})
    output.emit_record(closed, args.json)
    return 0


def _age(now: datetime, created_at: str) -> str:
    minutes = max(0, int((now - datetime.fromisoformat(created_at)).total_seconds()) // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    return f"{days}d{hours}h" if days else f"{hours}h{minutes}m" if hours else f"{minutes}m"
