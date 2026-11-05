import argparse
import json
from datetime import UTC, datetime
from typing import Any

from agent_board import identity, output
from agent_board.commands import Subcommands, add_command, board_client

LISTED_FILE = "inbox.json"
_COLUMNS = ("id", "reason", "source_ref", "summary")

type Wake = dict[str, Any]


def register(subcommands: Subcommands) -> None:
    inbox = add_command(subcommands, "inbox", "list the wakes leased to me; ack each once handled", _list)
    actions = inbox.add_subparsers(dest="inbox_command", metavar="ACTION")
    ack = add_command(actions, "ack", "acknowledge wakes so they are not delivered again", _ack)
    which = ack.add_mutually_exclusive_group(required=True)
    which.add_argument("ids", nargs="*", type=int, metavar="ID", default=[])
    which.add_argument("--all", action="store_true", help="every wake `inbox` has listed and not yet acked")


def _list(args: argparse.Namespace) -> int:
    me = identity.resolve(args.as_kind)
    with board_client(args, me) as client:
        newly_leased: list[Wake] = client.get("/api/wakes", wait=0)
    # A poll leases what it returns, so later polls skip these wakes until the lease runs out;
    # the local record keeps them listed and is where `ack --all` finds their ids.
    now = datetime.now(UTC)
    still_leased = [
        wake for wake in _load_listed(me.kind) if datetime.fromisoformat(wake["leased_until"]) > now
    ]
    wakes = sorted(
        {wake["id"]: wake for wake in [*still_leased, *newly_leased]}.values(), key=lambda wake: wake["id"]
    )
    _store_listed(me.kind, wakes)
    output.emit(wakes, args.json, columns=_COLUMNS)
    return 0


def _ack(args: argparse.Namespace) -> int:
    me = identity.resolve(args.as_kind)
    listed = _load_listed(me.kind)
    ids: list[int] = [wake["id"] for wake in listed] if args.all else args.ids
    acked = 0
    if ids:
        with board_client(args, me) as client:
            acked = client.post("/api/wakes/ack", {"ids": ids})["acked"]
    _store_listed(me.kind, [wake for wake in listed if wake["id"] not in ids])
    output.emit_record({"acked": acked}, args.json)
    return 0


def _load_listed(kind: str) -> list[Wake]:
    try:
        listed = json.loads((identity.state_dir(kind) / LISTED_FILE).read_text())
    except (OSError, ValueError):
        return []
    return listed if isinstance(listed, list) else []


def _store_listed(kind: str, wakes: list[Wake]) -> None:
    (identity.ensure_state_dir(kind) / LISTED_FILE).write_text(json.dumps(wakes))
