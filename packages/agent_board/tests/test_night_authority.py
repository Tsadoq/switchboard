import base64
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_board.night.board import SignedBoard
from agent_board.night.sync import BoardRefused, BoardUnavailable
from agent_board.night_authority import GrantUnavailable, NightAuthorityClient


@dataclass(frozen=True)
class Config:
    principal: str = "verifier@dev"
    box: str = "dev"
    namespace: str = "switchboard"
    board_url: str = "https://board.example.com"
    ca_path: Path | None = None
    signer_key: Path = Path("/etc/switchboard-night/verifier_ed25519")


def grant() -> dict[str, Any]:
    return {
        "principal": "pi@dev",
        "box": "dev",
        "state": "admitted",
        "remaining_seconds": 1200,
        "window_end_utc": 2200,
    }


def test_client_uses_signed_dedicated_endpoint_with_fresh_nonce() -> None:
    offered: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/night/verifier"
        assert "authorization" not in request.headers
        wrapper = json.loads(request.content)
        assert wrapper["signature"] == "test-signature"
        offered.append(json.loads(base64.urlsafe_b64decode(wrapper["envelope"])))
        return httpx.Response(200, json=grant())

    with httpx.Client(base_url=Config().board_url, transport=httpx.MockTransport(handle)) as http:
        client = NightAuthorityClient(Config(), http=http, signer=lambda _: "test-signature")
        assert client.fetch_grant("run", "fence").principal == "pi@dev"
        client.fetch_grant("run", "fence")
    assert offered[0]["principal"] == "verifier@dev" and offered[0]["action"] == "night.grant"
    assert offered[0]["target"] == "run" and offered[0]["payload"] == {"fence": "fence"}
    assert offered[0]["nonce"] != offered[1]["nonce"]


@pytest.mark.parametrize(
    "change",
    [
        {"remaining_seconds": float("nan")},
        {"remaining_seconds": True},
        {"remaining_seconds": 1201},
        {"remaining_seconds": -1},
        {"remaining_seconds": 10**400},
        {"box": "vault"},
        {"window_end_utc": float("inf")},
        {"extra": "ignored"},
    ],
)
def test_bad_grants_are_refused(change: dict[str, Any]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.dumps(grant() | change).encode()
        return httpx.Response(200, content=body)

    with httpx.Client(base_url=Config().board_url, transport=httpx.MockTransport(handle)) as http:
        client = NightAuthorityClient(Config(), http=http, signer=lambda _: "test-signature")
        with pytest.raises(GrantUnavailable):
            client.fetch_grant("run", "fence")


@pytest.mark.parametrize(
    "url",
    [
        "http://board.example.com",
        "https://user:password@board.example.com",
        "https://board.example.com?token=x",
        "https://board.example.com#x",
    ],
)
def test_unsafe_authority_urls_are_rejected(url: str) -> None:
    with pytest.raises(GrantUnavailable, match="unsafe_board_url"):
        NightAuthorityClient(replace(Config(), board_url=url))


def test_redirect_is_not_followed_and_denial_is_redacted() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example.com"})

    with httpx.Client(base_url=Config().board_url, transport=httpx.MockTransport(handle)) as http:
        client = NightAuthorityClient(Config(), http=http, signer=lambda _: "test-signature")
        with pytest.raises(GrantUnavailable, match="authority_unavailable"):
            client.fetch_grant("run", "secret-fence")
    assert len(calls) == 1


@pytest.mark.parametrize("status,error", [(401, BoardRefused), (409, BoardRefused), (503, BoardUnavailable)])
def test_board_adapter_distinguishes_authority_refusal_from_outage(
    status: int, error: type[Exception]
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"reason": "secret-do-not-log"})

    with httpx.Client(base_url=Config().board_url, transport=httpx.MockTransport(handle)) as http:
        board = SignedBoard(
            "dev", NightAuthorityClient(Config(), http=http, signer=lambda _: "test-signature")
        )
        with pytest.raises(error) as caught:
            board.state()
    assert "secret-do-not-log" not in str(caught.value)


@pytest.mark.parametrize(
    "change",
    [
        {"box": "vault"},
        {"counter": True},
        {"counter": -1},
        {"journal_id": "bad"},
        {"active": [{"id": "bad"}]},
        {"active": [{"id": "00000000-0000-4000-8000-000000000001"}]},
        {"active": [{"id": "00000000-0000-4000-8000-000000000001", "state": ""}]},
        {"active": [{"id": "00000000-0000-4000-8000-000000000001", "state": 1}]},
    ],
)
def test_board_adapter_rejects_malformed_state(change: dict[str, Any]) -> None:
    state = {"box": "dev", "journal_id": None, "counter": 0, "active": []} | change

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=state)

    with httpx.Client(base_url=Config().board_url, transport=httpx.MockTransport(handle)) as http:
        board = SignedBoard(
            "dev", NightAuthorityClient(Config(), http=http, signer=lambda _: "test-signature")
        )
        with pytest.raises(BoardRefused):
            board.state()


def test_unprivileged_default_signer_never_creates_a_key_or_sends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    key = tmp_path / "missing-key"

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request sent without root signer")

    with httpx.Client(base_url=Config().board_url, transport=httpx.MockTransport(forbidden)) as http:
        client = NightAuthorityClient(replace(Config(), signer_key=key), http=http)
        with pytest.raises(GrantUnavailable, match="root_signer_required"):
            client.fetch_grant("run", "fence")
    assert not key.exists()


def test_board_adapter_reports_only_server_stopping_rows_as_stop_requests() -> None:
    running, stopping = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
    state = {
        "box": "dev",
        "journal_id": None,
        "counter": 0,
        "active": [{"id": running, "state": "running"}, {"id": stopping, "state": "stopping"}],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=state)

    with httpx.Client(base_url=Config().board_url, transport=httpx.MockTransport(handle)) as http:
        board = SignedBoard(
            "dev", NightAuthorityClient(Config(), http=http, signer=lambda _: "test-signature")
        )
        fresh = board.state()

    assert fresh.active_run_ids == {running, stopping}
    assert fresh.stop_requested == {stopping}


def _capture_reply(**changes: Any) -> dict[str, Any]:
    return {
        "journal_id": "00000000-0000-4000-8000-000000000009",
        "counter": 3,
        "capture_id": "00000000-0000-4000-8000-000000000008",
        "run_id": "00000000-0000-4000-8000-000000000001",
        "principal": "pi@dev",
        "deadline": 5_000.0,
        "wakes": [],
        **changes,
    }


def _capture_with(reply: dict[str, Any]) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply)

    with httpx.Client(base_url=Config().board_url, transport=httpx.MockTransport(handle)) as http:
        board = SignedBoard(
            "dev", NightAuthorityClient(Config(), http=http, signer=lambda _: "test-signature")
        )
        return board.capture("00000000-0000-4000-8000-000000000001", {})


def test_board_adapter_returns_the_capture_snapshot_with_its_acknowledgement() -> None:
    reply = _capture_with(_capture_reply())

    assert (reply.ack.journal_id, reply.ack.counter) == ("00000000-0000-4000-8000-000000000009", 3)
    assert reply.snapshot["wakes"] == []


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(_capture_reply(fence="f" * 43), id="extra-field-such-as-a-fence"),
        pytest.param({k: v for k, v in _capture_reply().items() if k != "wakes"}, id="missing-wakes"),
        pytest.param(_capture_reply(counter=-1), id="bad-counter"),
        pytest.param(_capture_reply(journal_id="bad"), id="bad-journal"),
    ],
)
def test_board_adapter_refuses_a_capture_reply_outside_the_contract(reply: dict[str, Any]) -> None:
    with pytest.raises(BoardRefused):
        _capture_with(reply)
