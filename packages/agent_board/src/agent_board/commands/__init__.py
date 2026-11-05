import argparse
import io
import select
import sys
from collections.abc import Callable
from pathlib import Path

from agent_board import config, identity
from agent_board.client import Client
from agent_board.identity import Identity

STDIN_WAIT_SECONDS = 2

type Handler = Callable[[argparse.Namespace], int]
type Subcommands = argparse._SubParsersAction[argparse.ArgumentParser]


class CommandError(Exception):
    pass


def global_options(*, after_command: bool) -> argparse.ArgumentParser:
    # Repeated on every subcommand with SUPPRESS defaults, so an option given after the
    # subcommand wins and one given only before it is not reset by the subcommand's parser.
    unset = argparse.SUPPRESS if after_command else None
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument(
        "--as", dest="as_kind", metavar="KIND", default=unset, help="act as KIND when AI_AGENT is unset"
    )
    options.add_argument(
        "--board", metavar="URL", default=unset, help="board URL, overriding AGENT_BOARD_URL and config.toml"
    )
    options.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if after_command else False,
        help="print JSON",
    )
    return options


def add_command(
    subcommands: Subcommands, name: str, summary: str, handler: Handler
) -> argparse.ArgumentParser:
    command = _subparser(subcommands, name, summary)
    command.set_defaults(handler=handler)
    return command


def add_group(subcommands: Subcommands, name: str, summary: str) -> Subcommands:
    group = _subparser(subcommands, name, summary)
    return group.add_subparsers(dest=f"{name}_command", required=True, metavar="ACTION")


def board_client(args: argparse.Namespace, me: Identity | None = None) -> Client:
    return Client(config.load(args.board), me or identity.resolve(args.as_kind))


def read_body(body: str | None, file: Path | None = None) -> str:
    text = _given_body(body, file)
    if not text.strip():
        raise CommandError("empty body: pass some text as an argument, with --file PATH, or on stdin")
    return text


def _given_body(body: str | None, file: Path | None) -> str:
    if body == "-":
        return sys.stdin.read()
    if body is not None:
        return body
    if file is not None:
        try:
            return file.read_text()
        except (OSError, UnicodeDecodeError) as error:
            raise CommandError(f"cannot read {file}: {error}") from None
    if sys.stdin.isatty() or not _stdin_ready():
        raise CommandError("no body: pass it as an argument, with --file PATH, or pipe it with BODY set to -")
    return sys.stdin.read()


def _stdin_ready() -> bool:
    try:
        readable, _, _ = select.select([sys.stdin], [], [], STDIN_WAIT_SECONDS)
    except (OSError, ValueError, io.UnsupportedOperation):
        return True
    return bool(readable)


def _subparser(subcommands: Subcommands, name: str, summary: str) -> argparse.ArgumentParser:
    return subcommands.add_parser(
        name, help=summary, description=summary, parents=[global_options(after_command=True)]
    )
