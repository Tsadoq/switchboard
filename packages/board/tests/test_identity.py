import base64
import dataclasses
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import pytest
from board.app import create_app, main
from board.constants import DEFAULT_NAMESPACE as NAMESPACE
from board.db import Database
from board.identity.registry import BadSignerLine, Registry
from board.identity.routes import current_principal, decode_envelope, encode_envelope
from board.identity.verifier import NONCE_RETENTION_SECONDS, Verified, Verifier
from board.selftest import SelfTestFailed
from board.settings import Settings
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.responses import Response

if TYPE_CHECKING:
    from .conftest import Signer, SignerFactory

WELL_FORMED_ED25519_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIMCZljTIAg43jo0bnWxId0gQ16R5OjvSzNTmr8xRm8sf"
HOUR = 3600


def _app(client: TestClient) -> FastAPI:
    assert isinstance(client.app, FastAPI)
    return client.app


async def _echo(verified: Verified) -> Response:
    return JSONResponse({"principal": verified.envelope.principal})


async def _whoami(principal: Annotated[str, Depends(current_principal)]) -> dict[str, str]:
    return {"principal": principal}


def _signed_body(envelope: bytes, signature: str) -> dict[str, str]:
    return {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}


def _challenge(client: TestClient, principal: str) -> str:
    r = client.post("/api/auth/challenge", json={"principal": principal})
    assert r.status_code == 200, f"challenge refused: {r.status_code} {r.text}"
    challenge: str = r.json()["challenge"]
    return challenge


def _login(client: TestClient, signer: "Signer") -> str:
    challenge = _challenge(client, signer.principal)
    r = client.post(
        "/api/auth/token", json=_signed_body(*signer.envelope("auth", "", {"challenge": challenge}))
    )
    assert r.status_code == 200, f"token refused: {r.status_code} {r.text}"
    token: str = r.json()["token"]
    return token


def _utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_signed_envelope_is_accepted_once_and_replay_rejected(
    client: TestClient, signer: "SignerFactory"
) -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).state.actions.register("echo", _echo)
    body = _signed_body(*alice.envelope("echo", "thread:1", {"text": "hello"}))

    first = client.post("/api/signed", json=body)
    second = client.post("/api/signed", json=body)

    assert first.status_code == 200, f"first signed post rejected: {first.status_code} {first.text}"
    assert second.status_code == 401, f"replay accepted: {second.status_code}"
    assert second.json()["reason"] == "replay", f"wrong rejection reason: {second.json()}"


def test_tampered_envelope_rejected_as_bad_signature(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).state.actions.register("echo", _echo)
    envelope, signature = alice.envelope("echo", "thread:1", {"text": "hello"})
    tampered = envelope.replace(b"hello", b"jello")

    r = client.post("/api/signed", json=_signed_body(tampered, signature))

    assert r.status_code == 401, f"tampered envelope accepted: {r.status_code} {r.text}"
    assert r.json()["reason"] == "bad_signature", f"wrong rejection reason: {r.json()}"


def test_unknown_principal_rejected(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    mallory = signer("pi@elsewhere")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).state.actions.register("echo", _echo)

    r = client.post("/api/signed", json=_signed_body(*mallory.envelope("echo", "thread:1", {})))

    assert r.status_code == 401, f"unregistered principal accepted: {r.status_code} {r.text}"
    assert r.json()["reason"] == "unknown_principal", f"wrong rejection reason: {r.json()}"


def test_unknown_action_rejected(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)

    r = client.post("/api/signed", json=_signed_body(*alice.envelope("no-such-action", "", {})))

    assert r.status_code == 400, f"unknown action not refused: {r.status_code} {r.text}"
    assert r.json()["reason"] == "unknown_action", f"wrong rejection reason: {r.json()}"


def test_stale_timestamp_rejected(client: TestClient, signer: "SignerFactory", settings: Settings) -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).state.actions.register("echo", _echo)
    twice_the_window_ago = datetime.now(UTC) - 2 * timedelta(seconds=settings.ts_window_seconds)

    r = client.post(
        "/api/signed", json=_signed_body(*alice.envelope("echo", "", {}, ts=_utc(twice_the_window_ago)))
    )

    assert r.status_code == 401, f"stale envelope accepted: {r.status_code} {r.text}"
    assert r.json()["reason"] == "stale_timestamp", f"wrong rejection reason: {r.json()}"


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"nonce": "abcdefgh"}, id="nonce-of-8-chars"),
        pytest.param({"v": 2}, id="version-2"),
        pytest.param({"ts": "2026-11-04 19:40:00"}, id="ts-not-rfc3339"),
    ],
)
def test_malformed_envelope_rejected(
    client: TestClient, signer: "SignerFactory", override: dict[str, object]
) -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).state.actions.register("echo", _echo)

    r = client.post("/api/signed", json=_signed_body(*alice.envelope("echo", "", {}, **override)))

    assert r.status_code == 401, f"malformed envelope {override} accepted: {r.status_code} {r.text}"
    assert r.json()["reason"] == "malformed", f"wrong rejection reason for {override}: {r.json()}"


def test_revoked_after_date_rejected(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    registry: Registry = _app(client).state.registry
    registry.add_line(alice.allowed_line)
    registry.revoke_after(alice.principal, datetime.now(UTC).date() - timedelta(days=1))
    _app(client).state.actions.register("echo", _echo)

    r = client.post("/api/signed", json=_signed_body(*alice.envelope("echo", "", {})))

    assert r.status_code == 401, f"revoked key accepted: {r.status_code} {r.text}"
    assert r.json()["reason"] == "bad_signature", f"wrong rejection reason: {r.json()}"


def test_denied_key_rejected_even_if_line_present(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    registry: Registry = _app(client).state.registry
    registry.add_line(alice.allowed_line)
    registry.deny_key(alice.allowed_line)
    _app(client).state.actions.register("echo", _echo)

    r = client.post("/api/signed", json=_signed_body(*alice.envelope("echo", "", {})))

    assert r.status_code == 401, f"denied key accepted: {r.status_code} {r.text}"
    assert r.json()["reason"] == "bad_signature", f"wrong rejection reason: {r.json()}"


def test_registry_creates_empty_deny_list_on_init(tmp_path: Path) -> None:
    deny_list = tmp_path / "revoked_keys"

    Registry(tmp_path)

    assert deny_list.is_file(), f"no deny list created in {sorted(p.name for p in tmp_path.iterdir())}"
    assert deny_list.read_bytes() == b"", f"fresh deny list is not empty: {deny_list.read_bytes()!r}"


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            f'claude@testbox namespaces="git" ssh-ed25519 {WELL_FORMED_ED25519_BLOB}', id="other-ns"
        ),
        pytest.param(f"claude@testbox ssh-ed25519 {WELL_FORMED_ED25519_BLOB}", id="no-namespaces-option"),
        pytest.param(f'* namespaces="{NAMESPACE}" ssh-ed25519 {WELL_FORMED_ED25519_BLOB}', id="wildcard"),
        pytest.param(f'claude namespaces="{NAMESPACE}" ssh-ed25519 {WELL_FORMED_ED25519_BLOB}', id="no-box"),
        pytest.param(f'claude@testbox namespaces="{NAMESPACE}" ssh-rsa {WELL_FORMED_ED25519_BLOB}', id="rsa"),
        pytest.param(
            f'claude@testbox namespaces="{NAMESPACE}" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5', id="short-key"
        ),
    ],
)
def test_add_line_rejects_wrong_shape(tmp_path: Path, line: str) -> None:
    registry = Registry(tmp_path)

    with pytest.raises(BadSignerLine):
        registry.add_line(line)

    assert registry.principals() == set(), f"rejected line {line!r} still registered: {registry.principals()}"


def test_import_signers_loads_pubkey_all_output(
    tmp_path: Path, signer: "SignerFactory", monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    pubkey_all = tmp_path / "pubkey-all.txt"
    pubkey_all.write_text(f"{signer('claude@testbox').allowed_line}\n{signer('pi@testbox').allowed_line}\n")
    monkeypatch.setenv("BOARD_STATE_DIR", str(state_dir))

    exit_code = main(["import-signers", str(pubkey_all)])

    assert exit_code == 0, f"import-signers exited {exit_code}"
    principals = Registry(state_dir).principals()
    assert principals == {"claude@testbox", "pi@testbox"}, f"imported principals wrong: {principals}"


def test_challenge_token_grants_read_access(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).add_api_route("/test/whoami", _whoami, methods=["GET"])
    token = _login(client, alice)

    r = client.get("/test/whoami", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, f"bearer read refused: {r.status_code} {r.text}"
    assert r.json()["principal"] == alice.principal, f"token resolved to the wrong principal: {r.json()}"


def test_wrong_challenge_rejected(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _challenge(client, alice.principal)
    forged = {"challenge": "not-the-challenge-the-board-issued"}

    r = client.post("/api/auth/token", json=_signed_body(*alice.envelope("auth", "", forged)))

    assert r.status_code == 401, f"mismatched challenge redeemed: {r.status_code} {r.text}"
    assert r.json()["reason"] == "bad_challenge", f"wrong rejection reason: {r.json()}"


def test_expired_challenge_rejected(client: TestClient, signer: "SignerFactory", settings: Settings) -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    challenge = _challenge(client, alice.principal)
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE challenges SET expires_at = ?", (time.time() - 1,))

    r = client.post(
        "/api/auth/token", json=_signed_body(*alice.envelope("auth", "", {"challenge": challenge}))
    )

    assert r.status_code == 401, f"expired challenge redeemed: {r.status_code} {r.text}"
    assert r.json()["reason"] == "bad_challenge", f"wrong rejection reason: {r.json()}"


def test_expired_token_rejected(client: TestClient, signer: "SignerFactory", settings: Settings) -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).add_api_route("/test/whoami", _whoami, methods=["GET"])
    token = _login(client, alice)
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE tokens SET expires_at = ?", (time.time() - 1,))

    r = client.get("/test/whoami", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 401, f"expired token accepted: {r.status_code} {r.text}"
    assert r.json()["reason"] == "bad_token", f"wrong rejection reason: {r.json()}"


def test_nonce_pruning_removes_rows_older_than_24h(
    client: TestClient, signer: "SignerFactory", settings: Settings
) -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).state.actions.register("echo", _echo)
    now = time.time()
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.executemany(
            "INSERT INTO nonces (principal, nonce, seen_at) VALUES (?, ?, ?)",
            [
                (alice.principal, "seen-25-hours-ago", now - 25 * HOUR),
                (alice.principal, "seen-23-hours-ago", now - 23 * HOUR),
            ],
        )

    r = client.post("/api/signed", json=_signed_body(*alice.envelope("echo", "", {})))

    assert r.status_code == 200, f"verify that should prune was rejected: {r.status_code} {r.text}"
    with closing(sqlite3.connect(settings.db_path)) as conn:
        kept = {nonce for (nonce,) in conn.execute("SELECT nonce FROM nonces WHERE nonce LIKE 'seen-%'")}
    assert kept == {"seen-23-hours-ago"}, f"pruning kept the wrong nonces: {sorted(kept)}"


def test_verifier_refuses_window_wider_than_half_nonce_retention(tmp_path: Path) -> None:
    window = NONCE_RETENTION_SECONDS // 2 + 1

    with pytest.raises(ValueError, match="half"):
        Verifier(Registry(tmp_path), Database(tmp_path / "board.sqlite3"), window, "ssh-keygen")


def test_selftest_failure_prevents_startup(settings: Settings) -> None:
    broken = dataclasses.replace(settings, ssh_keygen="/nonexistent/ssh-keygen")

    with pytest.raises(SelfTestFailed) as failure, TestClient(create_app(broken)):
        pass

    assert failure.value.step == "keygen", f"self-test failed at the wrong step: {failure.value}"


@pytest.mark.parametrize("envelope_bytes", [b"", b"\xfb\xff", b'{"action":"post"}', bytes(range(256))])
def test_encoded_envelope_decodes_to_the_same_bytes(envelope_bytes: bytes) -> None:
    encoded = encode_envelope(envelope_bytes)

    assert decode_envelope(encoded) == envelope_bytes, f"round trip changed the envelope: {encoded!r}"
    assert set(encoded) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_="), encoded


def test_second_challenge_does_not_invalidate_first(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    first = _challenge(client, alice.principal)
    _challenge(client, alice.principal)

    r = client.post("/api/auth/token", json=_signed_body(*alice.envelope("auth", "", {"challenge": first})))

    assert r.status_code == 200, f"earlier challenge was overwritten: {r.status_code} {r.text}"


def test_challenge_is_consumed_once(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    challenge = _challenge(client, alice.principal)
    client.post("/api/auth/token", json=_signed_body(*alice.envelope("auth", "", {"challenge": challenge})))

    r = client.post(
        "/api/auth/token", json=_signed_body(*alice.envelope("auth", "", {"challenge": challenge}))
    )

    assert r.status_code == 401, f"challenge redeemed twice: {r.status_code} {r.text}"
    assert r.json()["reason"] in {"bad_challenge", "replay"}, f"wrong rejection reason: {r.json()}"


def test_challenge_for_another_principal_rejected(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    bob = signer("pi@testbox")
    _app(client).state.registry.add_line(alice.allowed_line)
    _app(client).state.registry.add_line(bob.allowed_line)
    bobs = _challenge(client, bob.principal)

    r = client.post("/api/auth/token", json=_signed_body(*alice.envelope("auth", "", {"challenge": bobs})))

    assert r.status_code == 401, f"another principal's challenge redeemed: {r.status_code} {r.text}"
    assert r.json()["reason"] == "bad_challenge", f"wrong rejection reason: {r.json()}"


def test_challenge_for_unknown_principal_refused(client: TestClient, settings: Settings) -> None:
    r = client.post("/api/auth/challenge", json={"principal": "nobody@nowhere"})

    assert r.status_code == 401, f"challenge issued to an unknown principal: {r.status_code} {r.text}"
    assert r.json()["reason"] == "unknown_principal", f"wrong rejection reason: {r.json()}"
    with closing(sqlite3.connect(settings.db_path)) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0]
    assert rows == 0, f"unknown principal left {rows} challenge rows"
