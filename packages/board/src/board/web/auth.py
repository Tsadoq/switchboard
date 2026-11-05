import asyncio
import hashlib
import secrets
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from board import passwords
from board.constants import HUMAN
from board.db import Database
from board.identity.routes import current_principal
from board.identity.verifier import Rejected
from board.settings import Settings

SESSION_COOKIE = "board_session"
SESSION_TTL_SECONDS = 30 * 86400
_RANDOM_BYTES = 32
FAILURES_PER_ADDRESS = 5
FAILURES_OVERALL = 20
FAILURE_WINDOW_SECONDS = 60
_CONCURRENT_HASHES = 2
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})


class LoginRequired(Exception):
    pass


class CrossSite(Exception):
    pass


class LoginThrottled(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


class FailedLogins:
    def __init__(
        self,
        per_address: int = FAILURES_PER_ADDRESS,
        overall: int = FAILURES_OVERALL,
        window_seconds: float = FAILURE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_address = per_address
        self._overall = overall
        self._window = window_seconds
        self._clock = clock
        self._by_address: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def retry_after(self, address: str) -> int:
        now = self._clock()
        self._forget(now)
        waits = [
            self._wait(failures, limit, now)
            for failures, limit in (
                (self._by_address.get(address), self._per_address),
                (self._all, self._overall),
            )
        ]
        return max(waits)

    def record(self, address: str) -> None:
        now = self._clock()
        self._by_address.setdefault(address, deque()).append(now)
        self._all.append(now)

    def clear(self, address: str) -> None:
        self._by_address.pop(address, None)

    def _forget(self, now: float) -> None:
        cutoff = now - self._window
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        for address in list(self._by_address):
            failures = self._by_address[address]
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                del self._by_address[address]

    def _wait(self, failures: deque[float] | None, limit: int, now: float) -> int:
        if failures is None or len(failures) < limit:
            return 0
        return max(1, int(failures[-limit] + self._window - now + 1))


class HumanSessions:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._password_hash = settings.human_password_hash
        self._secure_cookie = settings.public_url.startswith("https://")
        self._failures = FailedLogins()
        self._hashing = asyncio.Semaphore(_CONCURRENT_HASHES)

    async def log_in(self, address: str, password: str, response: Response) -> bool:
        retry_after = self._failures.retry_after(address)
        if retry_after:
            raise LoginThrottled(retry_after)
        if not await self._password_matches(password):
            self._failures.record(address)
            return False
        self._failures.clear(address)
        session = secrets.token_urlsafe(_RANDOM_BYTES)
        now = time.time()

        def store(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM human_sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO human_sessions (id, created_at, expires_at) VALUES (?, ?, ?)",
                (_sha256(session), now, now + SESSION_TTL_SECONDS),
            )

        await self._db.transact(store)
        response.set_cookie(
            SESSION_COOKIE,
            session,
            max_age=SESSION_TTL_SECONDS,
            secure=self._secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return True

    async def _password_matches(self, password: str) -> bool:
        if self._password_hash is None:
            return False
        async with self._hashing:
            return await run_in_threadpool(passwords.verify_password, password, self._password_hash)

    async def log_out(self, request: Request, response: Response) -> None:
        session = request.cookies.get(SESSION_COOKIE)
        if session:

            def remove(conn: sqlite3.Connection) -> None:
                conn.execute("DELETE FROM human_sessions WHERE id = ?", (_sha256(session),))

            await self._db.transact(remove)
        response.delete_cookie(SESSION_COOKIE, secure=self._secure_cookie, httponly=True, samesite="lax")

    async def is_logged_in(self, request: Request) -> bool:
        session = request.cookies.get(SESSION_COOKIE)
        if not session:
            return False

        def lookup(conn: sqlite3.Connection) -> bool:
            # Keyed by the cookie's SHA-256 for the same reason as read tokens: a stolen database
            # holds no usable session, and the index lookup cannot be steered byte by byte.
            row = conn.execute(
                "SELECT 1 FROM human_sessions WHERE id = ? AND expires_at > ?",
                (_sha256(session), time.time()),
            ).fetchone()
            return row is not None

        return await self._db.transact(lookup)


def human_sessions(request: Request) -> HumanSessions:
    sessions: HumanSessions = request.app.state.human_sessions
    return sessions


async def current_human(request: Request) -> str:
    if not await human_sessions(request).is_logged_in(request):
        raise LoginRequired
    return HUMAN


async def principal_or_human(request: Request) -> str:
    if "authorization" in request.headers:
        return await current_principal(request)
    if await human_sessions(request).is_logged_in(request):
        return HUMAN
    raise Rejected("bad_token")


async def same_origin(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        return
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        if site not in _SAME_ORIGIN_SITES:
            raise CrossSite
        return
    origin = request.headers.get("origin")
    if origin is not None and origin not in _own_origins(request):
        raise CrossSite


def _own_origins(request: Request) -> set[str]:
    settings: Settings = request.app.state.settings
    return {_origin(settings.public_url), _origin(str(request.base_url))}


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _sha256(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()
