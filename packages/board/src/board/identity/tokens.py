import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass

from board.db import Database
from board.identity import presence
from board.identity.verifier import Rejected, Verified

AUTH_ACTION = "auth"
CHALLENGE_TTL_SECONDS = 120
_RANDOM_BYTES = 32


@dataclass(frozen=True)
class Challenge:
    value: str
    expires_at: float


@dataclass(frozen=True)
class Token:
    value: str
    expires_at: float


class ReadTokens:
    def __init__(self, db: Database, token_ttl_hours: int) -> None:
        self._db = db
        self._token_ttl_seconds = token_ttl_hours * 3600

    async def issue_challenge(self, principal: str) -> Challenge:
        now = time.time()
        challenge = Challenge(secrets.token_urlsafe(_RANDOM_BYTES), now + CHALLENGE_TTL_SECONDS)

        def store(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM challenges WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO challenges (principal, challenge, expires_at) VALUES (?, ?, ?)",
                (principal, challenge.value, challenge.expires_at),
            )

        await self._db.transact(store)
        return challenge

    async def redeem(self, verified: Verified) -> Token:
        principal = verified.envelope.principal
        offered = verified.envelope.payload.get("challenge")
        if verified.envelope.action != AUTH_ACTION or not isinstance(offered, str) or not offered.isascii():
            raise Rejected("bad_challenge")
        now = time.time()
        token = Token(secrets.token_urlsafe(_RANDOM_BYTES), now + self._token_ttl_seconds)

        def exchange(conn: sqlite3.Connection) -> None:
            consumed = conn.execute(
                "DELETE FROM challenges WHERE challenge = ? AND principal = ? AND expires_at > ?",
                (offered, principal, now),
            ).rowcount
            if consumed != 1:
                raise Rejected("bad_challenge")
            conn.execute("DELETE FROM tokens WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO tokens (token_sha256, principal, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_sha256(token.value), principal, now, token.expires_at),
            )

        await self._db.transact(exchange)
        return token

    async def authenticate(self, bearer: str) -> str | None:
        now = time.time()

        def lookup(conn: sqlite3.Connection) -> str | None:
            # The lookup is keyed by the token's SHA-256, which a caller cannot steer byte by byte,
            # so the index comparison leaks nothing a constant-time compare would hide.
            row = conn.execute(
                "SELECT principal FROM tokens WHERE token_sha256 = ? AND expires_at > ?",
                (_sha256(bearer), now),
            ).fetchone()
            if row is None:
                return None
            presence.record_seen(conn, row["principal"], now)
            principal: str = row["principal"]
            return principal

        return await self._db.transact(lookup)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
