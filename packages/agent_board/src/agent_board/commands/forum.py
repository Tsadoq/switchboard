import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from agent_board import cursors, identity, output
from agent_board.client import Client
from agent_board.commands import Handler, Subcommands, add_command, board_client, read_body
from agent_board.cursors import CONSUMER_ENV, CURSOR_DIR, consumer_id

_HUMAN_DURATION = re.compile(
    r"(?:(?P<W>\d+)w)?(?:(?P<D>\d+)d)?(?:(?P<H>\d+)h)?(?:(?P<M>\d+)m)?(?:(?P<S>\d+)s)?"
)
_POSTED_FIELDS = ("thread_id", "post_id")
PAGE_MAX = 200
READ_LIMIT_MAX = 1000


class ThreadPages(Protocol):
    def get(self, path: str, **params: Any) -> Any: ...


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
    read.add_argument(
        "--after",
        type=_post_cursor,
        metavar="POST",
        help="only the posts after POST, oldest first; a wake names the post to start from",
    )
    read.add_argument(
        "--new",
        action="store_true",
        help=f"only posts this session has not printed yet; the session is named by ${CONSUMER_ENV}",
    )
    read.add_argument(
        "--start-after",
        type=_post_cursor,
        metavar="POST",
        help="with --new, also show posts after POST, even ones behind the cursor (a wake's starting point)",
    )
    read.add_argument(
        "--recover-after",
        type=_post_cursor,
        metavar="POST",
        help="with --new, replace an unreadable or missing cursor for this thread, then show posts after POST",
    )
    read.add_argument("--consumer", metavar="ID", help=f"the reading session, instead of ${CONSUMER_ENV}")
    read.add_argument(
        "--limit",
        type=_read_limit,
        metavar="N",
        help=f"with --after, at most N posts (1 to {READ_LIMIT_MAX}); prints how to continue when more remain",
    )
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
    if args.new:
        return _read_new(args)
    if args.start_after is not None or args.recover_after is not None or args.consumer is not None:
        raise SystemExit("agent-board read: --start-after, --recover-after and --consumer need --new")
    if args.limit is not None and args.after is None:
        raise SystemExit("agent-board read: --limit needs --after")
    with board_client(args) as client:
        thread = (
            client.get(f"/api/threads/{args.thread_id}") if args.after is None else _read_after(client, args)
        )
    output.emit_text(_thread_text(thread), thread, args.json)
    return 0


def _read_new(args: argparse.Namespace) -> int:
    """Shows what this session has not printed, then records it, only once it was written out and flushed."""
    if args.after is not None:
        raise SystemExit("agent-board read: --new reads after its own cursor; use --start-after, not --after")
    me = identity.resolve(args.as_kind)
    try:
        consumer = consumer_id(args.consumer or os.environ.get(CONSUMER_ENV))
        with cursors.locked(identity.ensure_state_dir(me.kind) / CURSOR_DIR, consumer) as positions:
            if positions.retired:
                raise SystemExit(
                    "agent-board read: this session was handed over to another; it reads no more"
                )
            base, shown = _new_boundary(positions, args)
            with board_client(args, me) as client:
                thread = _read_after(
                    client, argparse.Namespace(thread_id=args.thread_id, after=base, limit=args.limit)
                )
            for post in thread["posts"]:
                post["behind_cursor"] = shown is not None and post["id"] <= shown
            thread["new"] = True
            try:
                output.emit_text(_thread_text(thread), thread, args.json)
                sys.stdout.flush()
            except OSError:
                return 1
            if thread["posts"]:
                positions.advance(args.thread_id, thread["posts"][-1]["id"])
    except cursors.CursorError as error:
        raise SystemExit(f"agent-board read: {error}") from None
    return 0


def _new_boundary(positions: cursors.Cursors, args: argparse.Namespace) -> tuple[int, int | None]:
    """Where to read from, and the stored position, so replayed posts can be marked as behind the cursor.

    Behind the cursor is not "printed before": a cursor that started at a wake's boundary never showed older posts.
    """
    try:
        stored = positions.get(args.thread_id)
    except cursors.CursorCorrupt:
        if args.recover_after is None:
            raise SystemExit(
                f"agent-board read: the read cursor for thread {args.thread_id} is unreadable; recover it with "
                f"`agent-board read {args.thread_id} --new --recover-after <post>`"
            ) from None
        positions.set_aside(args.thread_id)
        return args.recover_after, None
    if args.recover_after is not None:
        if stored is not None:
            raise SystemExit(
                "agent-board read: this thread's read cursor is valid; there is nothing to recover"
            )
        return args.recover_after, None
    if stored is None and args.start_after is None:
        raise SystemExit(
            f"agent-board read: no read cursor yet for thread {args.thread_id}; "
            "give the wake's --start-after, or use --after"
        )
    if stored is None:
        return args.start_after, None
    if args.start_after is None:
        return stored, stored
    return min(stored, args.start_after), stored


def _read_after(client: ThreadPages, args: argparse.Namespace) -> dict[str, Any]:
    """Follows the server's pages from `--after` until the newest post or the limit, oldest first."""
    wanted = args.limit if args.limit is not None else READ_LIMIT_MAX
    posts: list[dict[str, Any]] = []
    cursor = args.after
    more = False
    thread: dict[str, Any] = {}
    while len(posts) < wanted:
        thread = client.get(
            f"/api/threads/{args.thread_id}",
            after=cursor,
            limit=min(PAGE_MAX, wanted - len(posts)),
        )
        page_ids = [post["id"] for post in thread["posts"]]
        if page_ids and (page_ids[0] <= cursor or page_ids != sorted(set(page_ids))):
            raise SystemExit(
                f"agent-board read: the board returned a page that does not advance past post {cursor}"
            )
        posts.extend(thread["posts"])
        more = bool(thread["page"]["newer"]) and bool(thread["posts"])
        if not more:
            break
        cursor = thread["posts"][-1]["id"]
    return {key: value for key, value in thread.items() if key != "page"} | {
        "posts": posts,
        "after": args.after,
        "more_after": cursor if more else None,
    }


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
    if "after" in thread:
        shown = len(thread["posts"])
        lines.append(f"{shown} new post{'' if shown == 1 else 's'} after post {thread['after']}")
    for post in thread["posts"]:
        marker = "verified" if post["verified"] else "unsigned"
        if post.get("behind_cursor"):
            marker += ", at or behind your read cursor"
        byline = f"by {post['author']} ({post['author_kind']}) at {post['created_at']}"
        lines += ["", f"--- post {post['id']} {byline} [{marker}]", post["body"].rstrip("\n")]
        if post["files"]:
            lines.append(f"files: {', '.join(post['files'])}")
    if thread.get("more_after") is not None:
        more = thread["more_after"]
        lines += ["", f"more posts follow: agent-board read {thread['id']} --after {more}"]
    return "\n".join(lines)


def _post_cursor(text: str) -> int:
    if re.fullmatch(r"0|[1-9][0-9]{0,18}", text) is None or int(text) > 2**63 - 1:
        raise argparse.ArgumentTypeError(f"not a post id: {text!r}")
    return int(text)


def _read_limit(text: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,3}", text) is None or int(text) > READ_LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"--limit must be 1 to {READ_LIMIT_MAX}: {text!r}")
    return int(text)


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
