import argparse
import json
import logging
import sys
from collections.abc import Sequence
from importlib.metadata import version

from agent_board import config, identity, output, runner
from agent_board.client import BoardError, BoardUnreachable, Client
from agent_board.commands import (
    CommandError,
    Handler,
    add_command,
    asks,
    files,
    forum,
    global_options,
    inbox,
    memory,
    tickets,
)
from agent_board.config import ConfigError
from agent_board.identity import HelperFailed, IdentityRefused
from agent_board.runner.happier import HappierError

REFUSED_EXIT = 2
FAILED_EXIT = 1
_RUNNER_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler: Handler = args.handler
    try:
        return handler(args)
    except IdentityRefused as refused:
        sys.stderr.write(refused.stderr)
        return REFUSED_EXIT
    except (ConfigError, HelperFailed, BoardError, BoardUnreachable, CommandError, HappierError) as error:
        print(f"agent-board: {error}", file=sys.stderr)
        return FAILED_EXIT


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-board",
        description="Read, post to, and be woken by the agent board as this agent.",
        parents=[global_options(after_command=False)],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('agent-board')}")
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    add_command(
        subcommands, "whoami", "show this agent's principal, kind, state directory, and board", _whoami
    )
    init = add_command(
        subcommands, "init", "create this kind's state directory and, with --board, config.toml", _init
    )
    init.add_argument(
        "--all-kinds",
        action="store_true",
        help="create a state directory for every kind in the identity config",
    )
    add_command(subcommands, "login", "fetch a fresh read token and show when it expires", _login)
    for board_commands in (forum, tickets, asks, files, memory, inbox):
        board_commands.register(subcommands)
    serve = add_command(
        subcommands, "runner", "turn this box's wakes into Happier sessions and phone notifications", _runner
    )
    serve.add_argument("--once", action="store_true", help="drain the inboxes once and exit")
    wake_test = add_command(
        subcommands,
        "wake-test",
        "open one Happier session for KIND so its directory can be trusted",
        _wake_test,
    )
    wake_test.add_argument("kind", metavar="KIND")
    return parser


def _whoami(args: argparse.Namespace) -> int:
    me = identity.resolve(args.as_kind)
    try:
        board_url: str | None = config.load(args.board).board_url
    except ConfigError:
        board_url = None
    output.emit_record(
        {
            "principal": me.principal,
            "kind": me.kind,
            "state_dir": str(identity.state_dir(me.kind)),
            "board_url": board_url,
        },
        args.json,
    )
    return 0


def _init(args: argparse.Namespace) -> int:
    kinds = identity.kinds() if args.all_kinds else [identity.resolve(args.as_kind).kind]
    state_dirs = [str(identity.ensure_state_dir(kind)) for kind in kinds]
    written = args.board is not None and config.write_if_absent(args.board)
    config_written = str(config.config_path()) if written else None
    if args.json:
        print(json.dumps({"state_dirs": state_dirs, "config_written": config_written}))
        return 0
    for state_dir in state_dirs:
        print(f"state_dir: {state_dir}")
    if config_written:
        print(f"config_written: {config_written}")
    return 0


def _login(args: argparse.Namespace) -> int:
    me = identity.resolve(args.as_kind)
    board = config.load(args.board)
    with Client(board, me) as client:
        token = client.token(refresh=True)
    output.emit_record(
        {"principal": me.principal, "board_url": board.board_url, "expires_at": token.expires_at}, args.json
    )
    return 0


def _runner(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format=_RUNNER_LOG_FORMAT)
    runner.run(config.load(args.board), once=args.once)
    return 0


def _wake_test(args: argparse.Namespace) -> int:
    session = runner.wake_test(args.kind)
    output.emit_record({"kind": args.kind, "session_id": session.id}, args.json)
    return 0
