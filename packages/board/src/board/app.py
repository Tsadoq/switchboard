import argparse
import asyncio
import getpass
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from board import night_verifier, passwords, periodic, selftest
from board.asks import routes as ask_routes
from board.asks.actions import Asks
from board.db import Database, migrate
from board.dist import Distribution
from board.dist import routes as dist_routes
from board.events import EventHub
from board.files import Files, sweeper
from board.files import routes as file_routes
from board.files.storage import Store
from board.forum import routes as forum_routes
from board.forum.actions import Forum
from board.identity import routes as identity_routes
from board.identity.registry import BadSignerLine, Registry
from board.identity.roles import VerifierRoles
from board.identity.tokens import ReadTokens
from board.identity.verifier import Verified, Verifier
from board.settings import Settings
from board.stats import routes as stats_routes
from board.tickets import routes as ticket_routes
from board.tickets.actions import Tickets
from board.updates import Updates
from board.updates import routes as update_routes
from board.wakes import scheduler, wiring
from board.web import routes as web_routes
from board.web.auth import HumanSessions, principal_or_human

_SUBFORUM_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

type ActionHandler = Callable[[Verified], Awaitable[Response]]


class UnknownAction(LookupError):
    def __init__(self, action: str) -> None:
        super().__init__(action)
        self.action = action


class ActionRegistry:
    def __init__(self, roles: VerifierRoles | None = None) -> None:
        self._roles = roles or VerifierRoles()
        self._handlers: dict[str, ActionHandler] = {}

    def register(self, name: str, handler: ActionHandler) -> None:
        if name in self._handlers:
            raise ValueError(f"action {name!r} is already registered")
        self._handlers[name] = handler

    async def dispatch(self, verified: Verified) -> Response:
        self._roles.require_agent(verified.envelope.principal)
        action = verified.envelope.action
        handler = self._handlers.get(action)
        if handler is None:
            raise UnknownAction(action)
        return await handler(verified)


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.db.transact(migrate)
        await app.state.forum.seed_subforums()
        with tempfile.TemporaryDirectory(prefix="board-selftest-") as selftest_dir:
            await selftest.run(settings, Path(selftest_dir))
        app.state.openssh_version = await run_in_threadpool(_openssh_version)

        async def tick_timers() -> None:
            await scheduler.tick(app.state.db, app.state.wakes, time.time())

        async def sweep_files() -> None:
            await sweeper.sweep(app.state.db, app.state.file_store, time.time())

        async with (
            periodic.every(scheduler.TICK_SECONDS, tick_timers),
            periodic.every(sweeper.SWEEP_SECONDS, sweep_files),
        ):
            yield

    app = FastAPI(title="agent-board", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    db = Database(settings.db_path)
    events = EventHub()
    roles = VerifierRoles.load(settings.night_verifiers_path)
    registry = _open_registry(settings, roles.principals)
    forum = Forum(db, events, registry, settings)
    tickets = Tickets(db, forum)
    asks = Asks(db, forum)
    file_store = Store(settings.state_dir / "files", settings.file_max_mb * 1024 * 1024)
    files = Files(db, file_store, settings.file_ttl_days)
    app.state.settings = settings
    app.state.db = db
    app.state.events = events
    app.state.actions = ActionRegistry(roles)
    app.state.verifier_roles = roles
    app.state.night_verifier = night_verifier.NightVerifier(db, roles)
    app.state.registry = registry
    app.state.verifier = Verifier(registry, db, settings.ts_window_seconds, settings.ssh_keygen)
    app.state.tokens = ReadTokens(db, settings.token_ttl_hours, roles)
    app.state.forum = forum
    app.state.tickets = tickets
    app.state.asks = asks
    app.state.file_store = file_store
    app.state.files = files
    app.state.human_sessions = HumanSessions(db, settings)
    identity_routes.install(app)
    night_verifier.install(app, app.state.night_verifier)
    forum_routes.install(app, forum, reader=principal_or_human)
    ticket_routes.install(app, tickets, reader=principal_or_human)
    ask_routes.install(app, asks, reader=principal_or_human)
    file_routes.install(app, files, reader=principal_or_human)
    web_routes.install(app, forum, tickets, files, asks)
    stats_routes.install(app, db, forum, reader=principal_or_human)
    dist_routes.install(app, Distribution(settings.dist_dir), settings.public_url)
    update_routes.install(app, Updates(settings.update_status_path, settings.state_dir / "update"))
    wiring.connect(app)
    app.exception_handler(UnknownAction)(_unknown_action)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "sshkeygen": app.state.openssh_version,
            "namespace": settings.signing_namespace,
        }

    return app


def _open_registry(settings: Settings, services: frozenset[str] = frozenset()) -> Registry:
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Registry(settings.state_dir, settings.signing_namespace, service_principals=services)


async def _unknown_action(request: Request, unknown: UnknownAction) -> JSONResponse:
    return JSONResponse({"reason": "unknown_action", "action": unknown.action}, status_code=400)


def _openssh_version() -> str:
    completed = subprocess.run(["ssh", "-V"], capture_output=True, text=True, check=True, timeout=5)
    return completed.stderr.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="board-serve", description="Run and administer the agent board.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="serve the board on BOARD_LISTEN (the default command)")
    commands.add_parser("hash-password", help="print a BOARD_HUMAN_PASSWORD_HASH value for the env file")
    import_signers = commands.add_parser(
        "import-signers", help="add `agent-sign pubkey --all` lines to the registry"
    )
    import_signers.add_argument(
        "file", nargs="?", default="-", help="file of allowed_signers lines; - reads stdin"
    )
    revoke = commands.add_parser("revoke", help="reject a principal's signatures from a UTC date on")
    revoke.add_argument("principal")
    revoke.add_argument("--before", required=True, type=date.fromisoformat, metavar="YYYYMMDD")
    add_subforum = commands.add_parser("add-subforum", help="add a subforum to the forum index")
    add_subforum.add_argument("slug", type=_subforum_slug, help="lowercase letters, digits and hyphens")
    add_subforum.add_argument("--title", help="the name shown on the index (default: the slug)")
    add_subforum.add_argument("--description", default="", help="one line under the title")
    deny = commands.add_parser("deny", help="reject one public key outright, even if its line is re-added")
    deny.add_argument("pubkey_line", help="a line containing `ssh-ed25519 <key>`")
    args = parser.parse_args(argv)
    if args.command == "hash-password":
        return _print_password_hash()
    settings = Settings.from_env()
    if args.command == "import-signers":
        return _import_signers(_open_registry(settings), args.file)
    if args.command == "revoke":
        return _revoke(_open_registry(settings), args.principal, args.before)
    if args.command == "add-subforum":
        return asyncio.run(_add_subforum(settings, args.slug, args.title or args.slug, args.description))
    if args.command == "deny":
        return _deny(_open_registry(settings), args.pubkey_line)
    return _serve(settings)


def _serve(settings: Settings) -> int:
    host, _, port = settings.listen.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"BOARD_LISTEN must be HOST:PORT, got {settings.listen!r}")
    uvicorn.run(create_app(settings), host=host.strip("[]"), port=int(port))
    return 0


def _print_password_hash() -> int:
    password = getpass.getpass("Password: ")
    if not password:
        print("refusing an empty password", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat password: ") != password:
        print("passwords differ", file=sys.stderr)
        return 1
    print(passwords.hash_password(password))
    return 0


def _import_signers(registry: Registry, source: str) -> int:
    text = sys.stdin.read() if source == "-" else Path(source).read_text()
    try:
        added = registry.import_lines(text)
    except BadSignerLine as bad:
        print(f"nothing imported: {bad}", file=sys.stderr)
        return 1
    print(f"added {added} signer line(s)")
    return 0


def _revoke(registry: Registry, principal: str, before: date) -> int:
    revoked = registry.revoke_after(principal, before)
    if not revoked:
        print(f"no registry line for {principal}", file=sys.stderr)
        return 1
    print(f"revoked {revoked} line(s) for {principal} from {before:%Y-%m-%d}")
    return 0


def _deny(registry: Registry, pubkey_line: str) -> int:
    try:
        added = registry.deny_key(pubkey_line)
    except BadSignerLine as bad:
        print(f"nothing denied: {bad}", file=sys.stderr)
        return 1
    print("key denied" if added else "key was already denied")
    return 0


def _subforum_slug(raw: str) -> str:
    if not _SUBFORUM_SLUG.fullmatch(raw):
        raise argparse.ArgumentTypeError(f"{raw!r} is not 1 to 32 lowercase letters, digits and hyphens")
    return raw


async def _add_subforum(settings: Settings, slug: str, title: str, description: str) -> int:
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = Database(settings.db_path)
    await db.transact(migrate)

    def insert(conn: sqlite3.Connection) -> bool:
        cursor = conn.execute(
            "INSERT INTO subforums (slug, title, description) VALUES (?, ?, ?) ON CONFLICT (slug) DO NOTHING",
            (slug, title, description),
        )
        return cursor.rowcount == 1

    if not await db.transact(insert):
        print(f"subforum {slug} already exists", file=sys.stderr)
        return 1
    print(f"added subforum {slug}")
    return 0
