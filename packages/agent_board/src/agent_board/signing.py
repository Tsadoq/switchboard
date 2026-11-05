import secrets
from datetime import UTC, datetime
from typing import Any

import rfc8785

from agent_board.identity import Identity, agent_sign

ENVELOPE_VERSION = 1
_NONCE_BYTES = 24
_RFC3339_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

type Envelope = dict[str, Any]


def envelope(identity: Identity, action: str, target: str, payload: dict[str, Any]) -> Envelope:
    return {
        "v": ENVELOPE_VERSION,
        "principal": identity.principal,
        "ts": datetime.now(UTC).strftime(_RFC3339_UTC_FORMAT),
        "nonce": secrets.token_urlsafe(_NONCE_BYTES),
        "action": action,
        "target": target,
        "payload": payload,
    }


def canonical(envelope: Envelope) -> bytes:
    return rfc8785.dumps(envelope)


def sign(data: bytes, as_kind: str | None = None) -> str:
    return agent_sign("sign", *(["--as", as_kind] if as_kind else []), stdin=data).decode()
