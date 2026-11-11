import base64
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from board import night
from board.night_smoke_authority import SmokeConfig, admit, calendar, load, smoke_app
from fastapi.testclient import TestClient


def test_smoke_authority_uses_real_roles_and_separate_bootstrap_admission(
    tmp_path: Path, signer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from board import night_smoke_authority

    service = signer("verifier@dev")
    public = tmp_path / "verifier.pub"
    public.write_text(service.allowed_line)
    config = SmokeConfig(
        tmp_path, "dev", "pi@dev", time.time() - 1, 120, tmp_path / "cert", tmp_path / "key", public, 18443
    )
    monkeypatch.setenv("BOARD_STATE_DIR", "/must-not-use-production-state")
    monkeypatch.setattr(night_smoke_authority, "_protected", lambda *args, **kwargs: None)
    original = night.window_for

    def send(client: TestClient, action: str, target: str, payload: dict[str, Any]) -> Any:
        raw, signature = service.envelope(action, target, payload)
        return client.post(
            "/api/night/verifier",
            json={"envelope": base64.urlsafe_b64encode(raw).decode(), "signature": signature},
        )

    with calendar(config), TestClient(smoke_app(config)) as client:
        for name, module in tuple(sys.modules.items()):
            if name.startswith("board.") and hasattr(module, "window_for"):
                assert vars(module)["window_for"] == config.window, name
        assert client.portal is not None
        with pytest.raises(ValueError, match="not_bootstrapped"):
            client.portal.call(admit, config)
        journal = str(uuid4())
        bootstrapped = send(
            client, "night.bootstrap", "self", {"journal_id": journal, "receipt_id": str(uuid4())}
        )
        assert bootstrapped.status_code == 200 and bootstrapped.json()["active"] == []
        client.portal.call(admit, config)
        data = json.loads((tmp_path / "run.json").read_text())
        assert (tmp_path / "run.json").stat().st_mode & 0o777 == 0o600
        grant = send(client, "night.grant", data["run_id"], {"fence": data["fence"]})
        assert grant.status_code == 200
        assert grant.json()["state"] == "admitted" and 0 < grant.json()["remaining_seconds"] <= 120
        with pytest.raises(ValueError, match="already_admitted"):
            client.portal.call(admit, config)
    assert night.window_for is original
    assert (tmp_path / "board.sqlite3").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"duration_seconds": 121},
        {"duration_seconds": True},
        {"port": 0},
        {"port": True},
        {"starts_at": float("nan")},
        {"box": "ops"},
        {"principal": "verifier@dev"},
        {"tls_key": "/outside/key"},
        {"extra": "ignored"},
    ],
)
def test_fixture_config_refuses_unsafe_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    from board import night_smoke_authority

    monkeypatch.setattr(night_smoke_authority, "_protected", lambda *args, **kwargs: None)
    data = {
        "box": "dev",
        "principal": "pi@dev",
        "starts_at": 100,
        "duration_seconds": 120,
        "port": 18443,
        "tls_cert": str(tmp_path / "cert"),
        "tls_key": str(tmp_path / "key"),
        "verifier_public_key": str(tmp_path / "pub"),
    } | change
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load(path)


def test_fixture_calendar_never_renews_or_extends(tmp_path: Path) -> None:
    config = SmokeConfig(
        tmp_path, "dev", "pi@dev", 100, 120, tmp_path / "cert", tmp_path / "key", tmp_path / "pub", 18443
    )
    assert config.window(99) is None
    window = config.window(100)
    assert window is not None and window.end == 220
    assert config.window(220) is None
