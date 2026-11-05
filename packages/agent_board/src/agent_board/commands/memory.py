import argparse
from datetime import UTC, datetime
from pathlib import Path

from agent_board import identity, output
from agent_board.commands import CommandError, Subcommands, add_command, add_group

MEMORY_FILE = "memory.md"


def register(subcommands: Subcommands) -> None:
    actions = add_group(subcommands, "memory", "keep dated notes that only this kind reads, on this box")
    add = add_command(actions, "add", "append a dated note", _add)
    add.add_argument("words", nargs="+", metavar="TEXT")
    show = add_command(actions, "show", "print my notes, oldest first", _show)
    show.add_argument("--tail", type=_positive_int, metavar="N", help="only the last N notes")
    add_command(actions, "path", "print where my notes live", _path)


def _add(args: argparse.Namespace) -> int:
    note = _one_line(" ".join(args.words))
    if not note:
        raise CommandError("the note is empty")
    path = identity.ensure_state_dir(identity.resolve(args.as_kind).kind) / MEMORY_FILE
    line = f"- {datetime.now(UTC).date().isoformat()}: {note}"
    with path.open("a") as notes:
        notes.write(f"{line}\n")
    output.emit_record({"path": str(path), "added": line}, args.json)
    return 0


def _show(args: argparse.Namespace) -> int:
    path = _memory_path(args)
    lines = path.read_text().splitlines() if path.exists() else []
    shown = lines if args.tail is None else lines[-args.tail :]
    output.emit_text("\n".join(shown), {"path": str(path), "lines": shown}, args.json)
    return 0


def _path(args: argparse.Namespace) -> int:
    path = str(_memory_path(args))
    output.emit_text(path, {"path": path}, args.json)
    return 0


def _memory_path(args: argparse.Namespace) -> Path:
    return identity.state_dir(identity.resolve(args.as_kind).kind) / MEMORY_FILE


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _positive_int(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"not a positive whole number: {text!r}")
    return number
