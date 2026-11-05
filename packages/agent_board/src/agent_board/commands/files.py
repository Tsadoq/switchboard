import argparse
import hashlib
import mimetypes
import os
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote

from agent_board import output
from agent_board.client import Client
from agent_board.commands import CommandError, Subcommands, add_command, add_group, board_client

_FALLBACK_CONTENT_TYPE = "application/octet-stream"


def register(subcommands: Subcommands) -> None:
    actions = add_group(subcommands, "files", "hand files to other agents through the board")
    put = add_command(actions, "put", "upload a signed file and print its id and sha256", _put)
    put.add_argument("path", type=Path, metavar="PATH")
    put.add_argument(
        "--ttl-days", type=int, metavar="N", help="keep it N days; the board caps and defaults it"
    )
    get = add_command(actions, "get", "download a file, keeping it only if its sha256 matches", _get)
    get.add_argument("file_id", metavar="ID")
    get.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="PATH",
        help="file or directory to write; default: the uploaded file name in the current directory",
    )
    add_command(actions, "list", "list the files that have not expired, newest first", _list)


def _put(args: argparse.Namespace) -> int:
    path: Path = args.path
    sha256, size = _digest(path)
    declaration: dict[str, Any] = {
        "name": path.name,
        "content_type": _content_type(path),
        "size": size,
        "sha256": sha256,
    }
    if args.ttl_days is not None:
        declaration["ttl_days"] = args.ttl_days
    with board_client(args) as client:
        stored = client.signed_upload(declaration, path)
    output.emit_record(stored, args.json)
    return 0


def _get(args: argparse.Namespace) -> int:
    with board_client(args) as client:
        entry = _live_entry(client, args.file_id)
        destination = _destination(args.output, entry)
        _download_verified(client, entry, destination)
    output.emit_record(
        {"id": entry["id"], "path": str(destination), "sha256": entry["sha256"], "size": entry["size"]},
        args.json,
    )
    return 0


def _list(args: argparse.Namespace) -> int:
    with board_client(args) as client:
        entries = client.get("/api/files")
    output.emit(entries, args.json, columns=("id", "size", "uploaded_by", "expires_at", "name"))
    return 0


def _live_entry(client: Client, file_id: str) -> dict[str, Any]:
    # The board has no per-file metadata route and its download carries no digest,
    # so the listing is the only source of the sha256 a download is checked against.
    entry: dict[str, Any] | None = next(
        (entry for entry in client.get("/api/files") if entry["id"] == file_id), None
    )
    if entry is None:
        raise CommandError(f"no live file {file_id}: it never existed or has expired")
    return entry


def _destination(requested: Path | None, entry: dict[str, Any]) -> Path:
    safe_name = _safe_file_name(entry)
    if requested is None:
        if Path(safe_name).exists():
            raise CommandError(f"{safe_name} already exists; pass -o PATH to choose where to write")
        return Path(safe_name)
    return requested / safe_name if requested.is_dir() else requested


def _safe_file_name(entry: dict[str, Any]) -> str:
    # The uploader chooses the name, so it may carry a path in either separator style;
    # only its last component is used, and the board's own id when nothing usable is left.
    uploaded_name = PureWindowsPath(entry["name"]).name
    return entry["id"] if uploaded_name in ("", ".", "..") else uploaded_name


def _download_verified(client: Client, entry: dict[str, Any], destination: Path) -> None:
    try:
        descriptor, staged_name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.")
    except OSError as error:
        raise CommandError(f"cannot write {destination}: {error.strerror or error}") from None
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "wb") as sink:
            client.download(f"/api/files/{quote(entry['id'], safe='')}", sink)
        received, _ = _digest(staged)
        if received != entry["sha256"]:
            raise CommandError(
                f"sha256 mismatch for {entry['id']}: expected {entry['sha256']}, received {received}; "
                "nothing was written"
            )
        os.replace(staged, destination)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _digest(path: Path) -> tuple[str, int]:
    try:
        with path.open("rb") as blob:
            sha256 = hashlib.file_digest(blob, "sha256").hexdigest()
            return sha256, blob.tell()
    except OSError as error:
        raise CommandError(f"cannot read {path}: {error.strerror or error}") from None


def _content_type(path: Path) -> str:
    guessed, encoding = mimetypes.guess_type(path.name)
    return guessed if guessed is not None and encoding is None else _FALLBACK_CONTENT_TYPE
