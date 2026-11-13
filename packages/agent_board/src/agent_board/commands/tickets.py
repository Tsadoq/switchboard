import argparse
from typing import Any

from agent_board import identity, output
from agent_board.commands import Handler, Subcommands, add_command, add_group, board_client, forum, read_body

_COLUMNS = ("id", "column", "assignee", "thread_id", "title")


def register(subcommands: Subcommands) -> None:
    actions = add_group(
        subcommands, "tickets", "list, create, claim, move, and comment on scrum-board tickets"
    )
    listing = add_command(actions, "list", "list tickets, oldest first", _list)
    listing.add_argument("--column", metavar="COLUMN", help="only tickets in COLUMN")
    listing.add_argument("--mine", action="store_true", help="only tickets assigned to me")
    create = add_command(actions, "create", "open a ticket and its discussion thread", _create)
    create.add_argument("--title", required=True)
    create.add_argument("--column", metavar="COLUMN", help="starting column; the board defaults to backlog")
    create.add_argument("body", nargs="?", metavar="BODY", help="the opening post; the title when omitted")
    forum.add_broadcast_flag(create)
    for action, summary in (
        ("claim", "assign a ticket to me; refused when someone else holds it"),
        ("release", "unassign a ticket"),
    ):
        change = add_command(actions, action, summary, _assignment(f"ticket.{action}"))
        change.add_argument("ticket_id", type=int, metavar="ID")
    move = add_command(actions, "move", "move a ticket to another column", _move)
    move.add_argument("ticket_id", type=int, metavar="ID")
    move.add_argument("column", metavar="COLUMN")
    comment = add_command(actions, "comment", "reply on a ticket's thread", _comment)
    comment.add_argument("ticket_id", type=int, metavar="ID")
    comment.add_argument("body", nargs="?", metavar="BODY", help="the comment; read from stdin when omitted")
    forum.add_broadcast_flag(comment)


def _list(args: argparse.Namespace) -> int:
    me = identity.resolve(args.as_kind)
    filters = {"column": args.column, "assignee": me.principal if args.mine else None}
    with board_client(args, me) as client:
        tickets = client.get("/api/tickets", **{key: value for key, value in filters.items() if value})
    output.emit(tickets, args.json, columns=_COLUMNS)
    return 0


def _create(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"title": args.title, "body": forum.checked_body(args, args.body or args.title)}
    if args.column is not None:
        payload["column"] = args.column
    return _signed(args, "ticket.create", "", payload)


def _assignment(action: str) -> Handler:
    def handle(args: argparse.Namespace) -> int:
        return _signed(args, action, f"ticket:{args.ticket_id}", {})

    return handle


def _move(args: argparse.Namespace) -> int:
    return _signed(args, "ticket.move", f"ticket:{args.ticket_id}", {"column": args.column})


def _comment(args: argparse.Namespace) -> int:
    body = forum.checked_body(args, read_body(args.body))
    with board_client(args) as client:
        ticket = client.get(f"/api/tickets/{args.ticket_id}")
        posted = forum.reply(client, ticket["thread_id"], body)
    output.emit_record(posted, args.json)
    return 0


def _signed(args: argparse.Namespace, action: str, target: str, payload: dict[str, Any]) -> int:
    with board_client(args) as client:
        ticket = client.signed(action, target, payload)
    output.emit_record(ticket, args.json)
    return 0
