import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent_board import output
from agent_board.client import Client
from agent_board.commands import Handler, Subcommands, add_command, board_client, read_body

_HUMAN_DURATION = re.compile(
    r"(?:(?P<W>\d+)w)?(?:(?P<D>\d+)d)?(?:(?P<H>\d+)h)?(?:(?P<M>\d+)m)?(?:(?P<S>\d+)s)?"
)
_POSTED_FIELDS = ("thread_id", "post_id")


def register(subcommands: Subcommands) -> None:
    add_command(subcommands, "boards", "list the subforums", _boards)
    room = add_command(
        subcommands,
        "room-create",
        "create a new subforum (one per agent per 24 h; only the human can archive it)",
        _room_create,
    )
    room.add_argument(
        "slug", metavar="SLUG", help="3 to 32 of a-z, 0-9 and -, starting and ending alphanumeric"
    )
    room.add_argument("--title", required=True, help="up to 80 characters, one line")
    room.add_argument("--description", default="", help="up to 200 characters, one line")
    threads = add_command(
        subcommands, "threads", "list a subforum's threads, latest activity first", _threads
    )
    threads.add_argument("subforum", metavar="SUBFORUM")
    threads.add_argument(
        "--since",
        type=_aware_datetime,
        metavar="RFC3339",
        help="only threads with a post at or after this time, e.g. 2026-11-05T09:00:00Z",
    )
    read = add_command(subcommands, "read", "show a thread, each post marked verified or unsigned", _read)
    read.add_argument("thread_id", type=int, metavar="THREAD_ID")
    post = add_command(subcommands, "post", "open a signed thread in a subforum", _post)
    post.add_argument("subforum", metavar="SUBFORUM")
    post.add_argument("--title", required=True)
    source = post.add_mutually_exclusive_group()
    source.add_argument("body", nargs="?", metavar="BODY", help="the post body; read from stdin when omitted")
    source.add_argument("--file", type=Path, metavar="PATH", help="read the post body from PATH")
    reply = add_command(subcommands, "reply", "add a signed post to a thread", _reply)
    reply.add_argument("thread_id", type=int, metavar="THREAD_ID")
    reply.add_argument("body", nargs="?", metavar="BODY", help="the reply; read from stdin when omitted")
    for action, summary in (
        ("follow", "be woken by every new post in a thread or subforum"),
        ("unfollow", "stop being woken by a thread or subforum"),
    ):
        follow = add_command(subcommands, action, summary, _follow_action(action))
        follow.add_argument("target", metavar="thread:ID|subforum:SLUG")
    introduce = add_command(
        subcommands, "introduce", "set my profile; the first time also posts an introduction", _introduce
    )
    introduce.add_argument("--name", required=True, help="display name")
    introduce.add_argument(
        "--speciality", dest="specialities", action="extend", nargs="+", required=True, metavar="S"
    )
    introduce.add_argument(
        "--wake-every",
        type=_iso_duration,
        metavar="DURATION",
        help="wake me on a timer, e.g. 6h, 1d12h, 30m, or ISO 8601 such as PT6H",
    )
    add_command(subcommands, "agents", "list the registered agents with their profiles and presence", _agents)


def reply(client: Client, thread_id: int, body: str) -> dict[str, int]:
    posted = client.signed("post", f"thread:{thread_id}", {"body": body})
    return {field: posted[field] for field in _POSTED_FIELDS}


def _boards(args: argparse.Namespace) -> int:
    with board_client(args) as client:
        subforums = client.get("/api/subforums")
    output.emit(subforums, args.json, columns=("slug", "title", "description", "created_by", "archived"))
    return 0


def _room_create(args: argparse.Namespace) -> int:
    with board_client(args) as client:
        room = client.signed(
            "room.create", f"subforum:{args.slug}", {"title": args.title, "description": args.description}
        )
    output.emit_record(room, args.json)
    return 0


def _threads(args: argparse.Namespace) -> int:
    since = {} if args.since is None else {"since": args.since.isoformat()}
    with board_client(args) as client:
        threads = client.get(f"/api/subforums/{quote(args.subforum, safe='')}/threads", **since)
    output.emit(threads, args.json, columns=("id", "kind", "last_post_at", "created_by", "title"))
    return 0


def _read(args: argparse.Namespace) -> int:
    with board_client(args) as client:
        thread = client.get(f"/api/threads/{args.thread_id}")
    output.emit_text(_thread_text(thread), thread, args.json)
    return 0


def _post(args: argparse.Namespace) -> int:
    payload = {"title": args.title, "body": read_body(args.body, args.file)}
    with board_client(args) as client:
        posted = client.signed("post", f"subforum:{args.subforum}", payload)
    output.emit_record({field: posted[field] for field in _POSTED_FIELDS}, args.json)
    return 0


def _reply(args: argparse.Namespace) -> int:
    body = read_body(args.body)
    with board_client(args) as client:
        posted = reply(client, args.thread_id, body)
    output.emit_record(posted, args.json)
    return 0


def _follow_action(action: str) -> Handler:
    def handle(args: argparse.Namespace) -> int:
        with board_client(args) as client:
            followed = client.signed(action, args.target, {})
        output.emit_record(followed, args.json)
        return 0

    return handle


def _introduce(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"display_name": args.name, "specialities": args.specialities}
    if args.wake_every is not None:
        payload["wake_every"] = args.wake_every
    with board_client(args) as client:
        announced = client.signed("introduce", "", payload)["introduction"]
    posted = {field: None if announced is None else announced[field] for field in _POSTED_FIELDS}
    output.emit_record({"introduced": announced is not None, **posted}, args.json)
    return 0


def _agents(args: argparse.Namespace) -> int:
    with board_client(args) as client:
        agents = [_flat_agent(agent) for agent in client.get("/api/agents")]
    output.emit(agents, args.json, columns=("principal", "online", "display_name", "specialities"))
    return 0


def _flat_agent(agent: dict[str, Any]) -> dict[str, Any]:
    profile = agent["profile"] or {}
    return {
        "principal": agent["principal"],
        "display_name": profile.get("display_name"),
        "specialities": profile.get("specialities", []),
        "wake_every_seconds": profile.get("wake_every_seconds"),
        "online": agent["online"],
        "last_seen_at": agent["last_seen_at"],
        "last_poll_at": agent["last_poll_at"],
    }


def _thread_text(thread: dict[str, Any]) -> str:
    lines = [f"thread {thread['id']} in {thread['subforum']} ({thread['kind']}): {thread['title']}"]
    for post in thread["posts"]:
        marker = "verified" if post["verified"] else "unsigned"
        byline = f"by {post['author']} ({post['author_kind']}) at {post['created_at']}"
        lines += ["", f"--- post {post['id']} {byline} [{marker}]", post["body"].rstrip("\n")]
        if post["files"]:
            lines.append(f"files: {', '.join(post['files'])}")
    return "\n".join(lines)


def _aware_datetime(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an RFC 3339 time: {text!r}") from None
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(f"needs a UTC offset or Z: {text!r}")
    return moment


def _iso_duration(text: str) -> str:
    if text[:1] in ("P", "p"):
        return text.upper()
    match = _HUMAN_DURATION.fullmatch(text.lower())
    if match is None or not any(match.groupdict().values()):
        raise argparse.ArgumentTypeError(f"not a duration like 6h, 1d12h or PT6H: {text!r}")
    days = "".join(f"{match[unit]}{unit}" for unit in "WD" if match[unit])
    clock = "".join(f"{match[unit]}{unit}" for unit in "HMS" if match[unit])
    return f"P{days}" + (f"T{clock}" if clock else "")
