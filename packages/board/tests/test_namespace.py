import base64
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from board import passwords
from board.app import create_app
from board.constants import DEFAULT_NAMESPACE
from board.identity.registry import BadSignerLine, Registry
from board.settings import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from .conftest import SignerFactory

CUSTOM = "agents@board.example.com"


def _settings(tmp_path: Path, **extra: str) -> Settings:
    return Settings.from_env(
        {
            "BOARD_STATE_DIR": str(tmp_path / "state"),
            "BOARD_HUMAN_PASSWORD_HASH": passwords.hash_password("pw"),
        }
        | extra
    )


def _post_status(client: TestClient, signer: "SignerFactory", principal: str) -> int:
    each = signer(principal)
    assert isinstance(client.app, FastAPI)
    client.app.state.registry.add_line(
        each.allowed_line.replace(each.namespace, client.app.state.settings.signing_namespace)
    )
    envelope, signature = each.envelope("post", "subforum:general", {"title": "hi", "body": "hello"})
    body = {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}
    return client.post("/api/signed", json=body).status_code


def test_signing_namespace_defaults_to_switchboard(tmp_path: Path) -> None:
    assert DEFAULT_NAMESPACE == "switchboard"
    assert _settings(tmp_path).signing_namespace == "switchboard"


def test_healthz_reports_the_signing_namespace(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, BOARD_SIGNING_NAMESPACE=CUSTOM))) as client:
        body = client.get("/healthz").json()

    assert body["namespace"] == CUSTOM, f"healthz namespace wrong: {body}"


def test_board_accepts_only_signatures_in_its_configured_namespace(
    tmp_path: Path, signer_in: Callable[[str], "SignerFactory"]
) -> None:
    right = signer_in(CUSTOM)
    wrong = signer_in(DEFAULT_NAMESPACE)
    with TestClient(create_app(_settings(tmp_path, BOARD_SIGNING_NAMESPACE=CUSTOM))) as client:
        accepted = _post_status(client, right, "claude@laptop")
        refused = _post_status(client, wrong, "pi@laptop")

    assert accepted == 201, f"signature in the configured namespace refused: {accepted}"
    assert refused == 401, f"signature in another namespace accepted: {refused}"


def test_registry_refuses_lines_for_another_namespace(
    tmp_path: Path, signer_in: Callable[[str], "SignerFactory"]
) -> None:
    registry = Registry(tmp_path, CUSTOM)
    line = signer_in(DEFAULT_NAMESPACE)("claude@laptop").allowed_line

    with pytest.raises(BadSignerLine, match=CUSTOM):
        registry.add_line(line)


@pytest.mark.parametrize("value", ['has"quote', "has space", "a,b", "x" * 129])
def test_bad_signing_namespace_is_a_startup_error(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="BOARD_SIGNING_NAMESPACE"):
        _settings(tmp_path, BOARD_SIGNING_NAMESPACE=value)
