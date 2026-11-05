import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board import config, identity, signing
from agent_board.client import TOKEN_FILE, Client

if TYPE_CHECKING:
    from .conftest import AgentBoard, BoardServer, FakeSign

BOX = "testbox"
KINDS = ["claude", "pi"]


def _board_home() -> Path:
    return Path.home() / ".agents" / "board"


def test_whoami_resolves_kind_from_ai_agent_prefix(
    fake_sign: "FakeSign", agent_board: "AgentBoard", claude_ai_agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)

    result = agent_board("whoami")

    assert result.returncode == 0, f"whoami failed: {result.returncode} {result.stderr}"
    assert f"claude@{BOX}" in result.stdout, f"wrong principal: {result.stdout}"


def test_as_kind_resolves_principal_via_pubkey_without_ai_agent(
    fake_sign: "FakeSign", agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.delenv("AI_AGENT", raising=False)

    result = agent_board("whoami", "--as", "pi", "--json")

    assert result.returncode == 0, f"whoami --as pi failed: {result.returncode} {result.stderr}"
    reported = json.loads(result.stdout)
    assert (reported["principal"], reported["kind"]) == (f"pi@{BOX}", "pi"), f"wrong identity: {reported}"


def test_init_creates_state_dir_for_resolved_kind_with_mode_0700(
    fake_sign: "FakeSign", agent_board: "AgentBoard", claude_ai_agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)

    result = agent_board("init")

    assert result.returncode == 0, f"init failed: {result.returncode} {result.stderr}"
    state_dir = _board_home() / "claude"
    assert state_dir.is_dir(), f"no state dir at {state_dir}: {result.stdout}"
    mode = stat.S_IMODE(state_dir.stat().st_mode)
    assert mode == 0o700, f"state dir mode is {mode:o}"
    assert not (_board_home() / "pi").exists(), "init without --all-kinds created another kind's directory"


def test_init_all_kinds_creates_one_dir_per_configured_kind(
    fake_sign: "FakeSign", agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.delenv("AI_AGENT", raising=False)

    result = agent_board("init", "--all-kinds")

    assert result.returncode == 0, f"init --all-kinds failed: {result.returncode} {result.stderr}"
    created = {
        path.name: stat.S_IMODE(path.stat().st_mode) for path in _board_home().iterdir() if path.is_dir()
    }
    assert created == {"claude": 0o700, "pi": 0o700}, f"wrong state dirs: {created}"


def test_init_board_writes_config_only_when_absent(
    fake_sign: "FakeSign", agent_board: "AgentBoard", claude_ai_agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    first, second = "http://first.example:8080", "http://second.example:8080"

    initial = agent_board("init", "--all-kinds", "--board", first)
    repeated = agent_board("init", "--all-kinds", "--board", second)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    shown = agent_board("whoami", "--json")

    assert (initial.returncode, repeated.returncode) == (0, 0), (
        f"init failed: {initial.stderr} {repeated.stderr}"
    )
    assert shown.returncode == 0, f"whoami failed: {shown.returncode} {shown.stderr}"
    assert json.loads(shown.stdout)["board_url"] == first, f"config.toml overwritten: {shown.stdout}"


def test_helper_refusal_exit_2_is_passed_through_verbatim(
    fake_sign: "FakeSign", agent_board: "AgentBoard", claude_ai_agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    helper = subprocess.run(
        ["agent-sign", "--config", os.environ["AGENT_IDENTITY_CONF"], "pubkey", "--as", "pi"],
        capture_output=True,
        text=True,
        check=False,
    )

    result = agent_board("--as", "pi", "whoami")

    assert helper.returncode == 2 and helper.stderr, (
        f"helper did not refuse: {helper.returncode} {helper.stderr}"
    )
    assert result.returncode == 2, f"refusal not exit 2: {result.returncode} {result.stderr}"
    assert result.stderr == helper.stderr, f"refusal not verbatim: {result.stderr!r} != {helper.stderr!r}"


def test_canonical_bytes_follow_rfc8785() -> None:
    envelope = {"zeta": 1, "alpha": "café ☃", "mid": [3, {"b": True, "a": None}], "small": 1.5e-7}

    canonical = signing.canonical(envelope)

    expected = b'{"alpha":"caf\xc3\xa9 \xe2\x98\x83","mid":[3,{"a":null,"b":true}],"small":1.5e-7,"zeta":1}'
    assert canonical == expected, f"not RFC 8785: {canonical!r}"


def test_login_obtains_token_from_live_server(
    fake_sign: "FakeSign",
    agent_board: "AgentBoard",
    board_server: "BoardServer",
    claude_ai_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = fake_sign.configure(box=BOX, kinds=KINDS)
    board_server.registry.add_line(lines["claude"])
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    monkeypatch.setenv("AGENT_BOARD_URL", board_server.url)

    result = agent_board("login", "--json")

    assert result.returncode == 0, f"login failed: {result.returncode} {result.stderr}"
    expires_at = datetime.fromisoformat(json.loads(result.stdout)["expires_at"])
    assert expires_at > datetime.now(UTC), f"token already expired: {result.stdout}"


def test_login_refused_by_board_exits_1_with_reason(
    fake_sign: "FakeSign",
    agent_board: "AgentBoard",
    board_server: "BoardServer",
    claude_ai_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)

    result = agent_board("login", "--board", board_server.url)

    assert result.returncode == 1, f"unregistered login not exit 1: {result.returncode} {result.stderr}"
    assert "unknown_principal" in result.stderr, f"reason missing: {result.stderr}"


def test_token_is_reused_across_clients_until_refreshed(
    fake_sign: "FakeSign",
    board_server: "BoardServer",
    claude_ai_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = fake_sign.configure(box=BOX, kinds=KINDS)
    board_server.registry.add_line(lines["claude"])
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    board, me = config.load(board_server.url), identity.resolve()

    with Client(board, me) as first_client:
        first = first_client.token()
    with Client(board, me) as second_client:
        reused = second_client.token()
        refreshed = second_client.token(refresh=True)

    assert reused == first, f"cached token not reused: {first} then {reused}"
    assert refreshed.value != first.value, "refresh returned the cached token"


def test_get_retries_once_after_401_with_fresh_token(
    fake_sign: "FakeSign",
    agent_board: "AgentBoard",
    board_server: "BoardServer",
    claude_ai_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = fake_sign.configure(box=BOX, kinds=KINDS)
    board_server.registry.add_line(lines["claude"])
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    login = agent_board("login", "--board", board_server.url)
    assert login.returncode == 0, f"login failed: {login.returncode} {login.stderr}"
    cache = _board_home() / "claude" / TOKEN_FILE
    cached = json.loads(cache.read_text())
    cache.write_text(json.dumps(cached | {"token": "garbage"}))

    with Client(config.load(board_server.url), identity.resolve()) as client:
        follows = client.get("/api/me/follows")

    assert follows == [], f"get after 401 did not succeed: {follows}"
    assert json.loads(cache.read_text())["token"] not in ("garbage", cached["token"]), "token not refreshed"
