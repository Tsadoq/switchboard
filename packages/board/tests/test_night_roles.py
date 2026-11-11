import base64
import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from board.app import create_app
from board.identity.roles import VerifierRoles
from board.identity.verifier import Envelope, Rejected, Verified
from board.settings import Settings
from fastapi.testclient import TestClient

FINGERPRINT = "SHA256:" + "A" * 43


def verified(principal: str = "verifier@dev", fingerprint: str = FINGERPRINT) -> Verified:
    envelope = Envelope(principal, datetime.now(UTC), "n" * 24, "night.grant", "run", {})
    return Verified(envelope, b"", "", fingerprint)


def config(tmp_path: Path, value: Any) -> Path:
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(value))
    return path


def test_role_requires_exact_configured_identity_key_and_box(tmp_path: Path) -> None:
    roles = VerifierRoles.load(config(tmp_path, {"verifier@dev": {"box": "dev", "fingerprint": FINGERPRINT}}))
    assert roles.authorize(verified()) == "dev"
    assert roles.principals == frozenset({"verifier@dev"})
    for offered in (
        verified("pi@dev"),
        verified("verifier@vault"),
        verified(fingerprint="SHA256:" + "B" * 43),
    ):
        with pytest.raises(Rejected, match="forbidden_role"):
            roles.authorize(offered)
    with pytest.raises(Rejected, match="forbidden_role"):
        roles.require_agent("verifier@dev")
    roles.require_agent("pi@dev")
    with pytest.raises(Rejected, match="forbidden_role"):
        VerifierRoles.load(None).authorize(verified())


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"verifier@dev": {}},
        {"verifier@dev": {"box": "ops", "fingerprint": FINGERPRINT}},
        {"pi@dev": {"box": "dev", "fingerprint": FINGERPRINT}},
        {"verifier@dev": {"box": "dev", "fingerprint": "fake"}},
        {"verifier@dev": {"box": "dev", "fingerprint": FINGERPRINT, "admin": True}},
    ],
)
def test_bad_role_declarations_fail_closed(tmp_path: Path, value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        VerifierRoles.load(config(tmp_path, value))


def test_service_cannot_use_generic_actions_tokens_or_agent_registry(
    settings: Settings, signer: Any, tmp_path: Path
) -> None:
    service = signer("verifier@dev")
    blob = service.allowed_line.split()[-1]
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(blob)).digest()
    ).decode().rstrip("=")
    path = config(tmp_path, {"verifier@dev": {"box": "dev", "fingerprint": fingerprint}})
    app = create_app(replace(settings, night_verifiers_path=path))
    app.state.registry.add_line(service.allowed_line)
    with TestClient(app) as client:
        assert app.state.registry.known(service.principal)
        assert service.principal not in app.state.registry.principals()
        challenge = client.post("/api/auth/challenge", json={"principal": service.principal})
        assert challenge.status_code == 401 and challenge.json()["reason"] == "forbidden_role"
        raw, signature = service.envelope("forum.thread", "general", {})
        body = {"envelope": base64.urlsafe_b64encode(raw).decode(), "signature": signature}
        rejected = client.post("/api/signed", json=body)
        assert rejected.status_code == 401 and rejected.json()["reason"] == "forbidden_role"
        bearer = "old-role-token"
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "INSERT INTO tokens (token_sha256, principal, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (
                    hashlib.sha256(bearer.encode()).hexdigest(),
                    service.principal,
                    time.time(),
                    time.time() + 3600,
                ),
            )
        response = client.get("/api/agents", headers={"authorization": "Bearer " + bearer})
        assert response.status_code == 401


def test_role_file_rejects_duplicate_keys_and_nonfinite_json(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    for text in ('{"verifier@dev":{},"verifier@dev":{}}', '{"verifier@dev":NaN}'):
        path.write_text(text)
        with pytest.raises(ValueError):
            VerifierRoles.load(path)
