import json
import secrets
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from board import passwords
from board.app import create_app
from board.constants import DEFAULT_NAMESPACE
from board.settings import Settings
from fastapi.testclient import TestClient

TEST_PASSWORD = "correct horse"


@dataclass(frozen=True)
class Signer:
    principal: str
    key_path: Path
    allowed_line: str
    namespace: str = DEFAULT_NAMESPACE

    def sign(self, data: bytes) -> str:
        completed = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(self.key_path), "-n", self.namespace],
            input=data,
            capture_output=True,
            check=True,
            timeout=5,
        )
        return completed.stdout.decode()

    def envelope(
        self, action: str, target: str, payload: dict[str, Any], **overrides: object
    ) -> tuple[bytes, str]:
        fields = {
            "v": 1,
            "principal": self.principal,
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "nonce": secrets.token_urlsafe(24),
            "action": action,
            "target": target,
            "payload": payload,
        }
        data = json.dumps(fields | overrides, sort_keys=True, separators=(",", ":")).encode()
        return data, self.sign(data)


class SignerFactory:
    def __init__(self, key_dir: Path, namespace: str = DEFAULT_NAMESPACE) -> None:
        self._key_dir = key_dir
        self._namespace = namespace
        self._signers: dict[str, Signer] = {}

    def __call__(self, principal: str = "claude@testbox") -> Signer:
        if principal not in self._signers:
            self._signers[principal] = self._generate(principal)
        return self._signers[principal]

    def _generate(self, principal: str) -> Signer:
        self._key_dir.mkdir(exist_ok=True)
        key_path = self._key_dir / principal
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", principal, "-f", str(key_path)],
            check=True,
            timeout=5,
        )
        key_type, blob = Path(f"{key_path}.pub").read_text().split()[:2]
        allowed_line = f'{principal} namespaces="{self._namespace}" {key_type} {blob}'
        return Signer(principal, key_path, allowed_line, self._namespace)


@pytest.fixture
def human_password() -> str:
    return TEST_PASSWORD


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "BOARD_STATE_DIR": str(tmp_path / "state"),
            "BOARD_PUBLIC_URL": "https://board.example.com",
            "BOARD_HUMAN_PASSWORD_HASH": passwords.hash_password(TEST_PASSWORD),
            "BOARD_UPDATE_STATUS": str(tmp_path / "update-status.json"),
        }
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def signer(tmp_path: Path) -> SignerFactory:
    return SignerFactory(tmp_path / "signer-keys")


@pytest.fixture
def signer_in(tmp_path: Path) -> Callable[[str], SignerFactory]:
    def factory(namespace: str) -> SignerFactory:
        return SignerFactory(tmp_path / f"keys-{namespace}", namespace)

    return factory
