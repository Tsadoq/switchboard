import base64
import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Self

from starlette.concurrency import run_in_threadpool

from board.db import Database
from board.identity.registry import Registry

type RejectionReason = Literal[
    "malformed",
    "unknown_principal",
    "stale_timestamp",
    "bad_signature",
    "replay",
    "bad_challenge",
    "bad_token",
    "forbidden_role",
]

NONCE_RETENTION_SECONDS = 24 * 3600
SSH_KEYGEN_TIMEOUT_SECONDS = 5
_FIELDS = frozenset({"v", "principal", "ts", "nonce", "action", "target", "payload"})
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_RFC3339_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z")
_RFC3339_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class Rejected(Exception):
    def __init__(self, reason: RejectionReason) -> None:
        super().__init__(reason)
        self.reason: RejectionReason = reason


def rfc3339(epoch_seconds: float) -> str:
    return time.strftime(_RFC3339_UTC_FORMAT, time.gmtime(epoch_seconds))


@dataclass(frozen=True)
class Envelope:
    principal: str
    ts: datetime
    nonce: str
    action: str
    target: str
    payload: dict[str, Any]

    @classmethod
    def parse(cls, data: bytes) -> Self:
        try:
            fields = json.loads(data.decode(), object_pairs_hook=_unique_keys, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            raise Rejected("malformed") from None
        if not isinstance(fields, dict) or fields.keys() != _FIELDS:
            raise Rejected("malformed")
        version, principal, ts, nonce, action, target, payload = (
            fields[name] for name in ("v", "principal", "ts", "nonce", "action", "target", "payload")
        )
        well_formed = (
            type(version) is int
            and version == 1
            and isinstance(principal, str)
            and isinstance(ts, str)
            and _RFC3339_UTC.fullmatch(ts) is not None
            and isinstance(nonce, str)
            and _NONCE.fullmatch(nonce) is not None
            and isinstance(action, str)
            and action != ""
            and isinstance(target, str)
            and isinstance(payload, dict)
        )
        if not well_formed:
            raise Rejected("malformed")
        try:
            moment = datetime.fromisoformat(ts)
        except ValueError:
            raise Rejected("malformed") from None
        return cls(principal, moment, nonce, action, target, payload)


@dataclass(frozen=True)
class Verified:
    envelope: Envelope
    envelope_bytes: bytes
    signature: str
    key_fingerprint: str


class Verifier:
    def __init__(self, registry: Registry, db: Database, ts_window_seconds: int, ssh_keygen: str) -> None:
        if 2 * ts_window_seconds > NONCE_RETENTION_SECONDS:
            raise ValueError(
                f"ts_window_seconds={ts_window_seconds} must be at most half the {NONCE_RETENTION_SECONDS}s "
                "nonce retention: an envelope's ts may run one window ahead of the nonce's first sighting "
                "and stays fresh one window past it, so a nonce pruned sooner could be replayed"
            )
        self._registry = registry
        self._db = db
        self._ts_window_seconds = ts_window_seconds
        self._ssh_keygen = ssh_keygen

    async def verify(self, envelope_bytes: bytes, signature: str) -> Verified:
        envelope = Envelope.parse(envelope_bytes)
        if not signature.isascii():
            raise Rejected("malformed")
        now = time.time()
        if abs(now - envelope.ts.timestamp()) > self._ts_window_seconds:
            raise Rejected("stale_timestamp")
        public_key = await run_in_threadpool(
            self._check_signature, envelope.principal, envelope_bytes, signature
        )

        def record_nonce(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM nonces WHERE seen_at < ?", (now - NONCE_RETENTION_SECONDS,))
            try:
                conn.execute(
                    "INSERT INTO nonces (principal, nonce, seen_at) VALUES (?, ?, ?)",
                    (envelope.principal, envelope.nonce, now),
                )
            except sqlite3.IntegrityError:
                raise Rejected("replay") from None

        await self._db.transact(record_nonce)
        return Verified(envelope, envelope_bytes, signature, _fingerprint(public_key))

    def _check_signature(self, principal: str, envelope_bytes: bytes, signature: str) -> str:
        if not self._registry.known(principal):
            raise Rejected("unknown_principal")
        with tempfile.NamedTemporaryFile("w", suffix=".sig", delete_on_close=False) as sigfile:
            sigfile.write(signature)
            sigfile.close()
            completed = subprocess.run(
                [
                    self._ssh_keygen,
                    "-Y",
                    "verify",
                    *self._registry.verification_options(),
                    "-I",
                    principal,
                    "-n",
                    self._registry.namespace,
                    "-s",
                    sigfile.name,
                    "-O",
                    "print-pubkey",
                ],
                input=envelope_bytes,
                capture_output=True,
                timeout=SSH_KEYGEN_TIMEOUT_SECONDS,
                check=False,
            )
        if completed.returncode != 0:
            raise Rejected("bad_signature")
        return _printed_public_key(completed.stdout)


def _printed_public_key(stdout: bytes) -> str:
    for line in stdout.decode(errors="replace").splitlines():
        if line.startswith("ssh-"):
            return line.strip()
    raise RuntimeError(f"ssh-keygen accepted a signature without printing its public key: {stdout!r}")


def _fingerprint(public_key: str) -> str:
    digest = hashlib.sha256(base64.b64decode(public_key.split()[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError(f"duplicate keys in {keys}")
    return dict(pairs)


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")
