import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.config import ConfigError
from agent_board.runner.config import RunnerConfig
from agent_board.runner.loop import prompt_for

if TYPE_CHECKING:
    from .conftest import AgentBoard, BoardServer, FakeSign

BOX = "testbox"
KINDS = ["claude", "pi"]
CLAUDE = f"claude@{BOX}"
PI = f"pi@{BOX}"
AGENT_BOARD = Path(sys.executable).parent / "agent-board"


@dataclass(frozen=True)
class Fakes:
    happier_state: Path
    tmux_log: Path
    board_db: Path


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    happier_state = tmp_path / "happier-state.json"
    happier_state.write_text("")
    monkeypatch.setenv("FAKE_HAPPIER_STATE", str(happier_state))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(tmp_path / "tmux.log"))
    monkeypatch.setenv("AGENT_BOARD_RUNNER_CONF", str(tmp_path / "agent-board-runner.toml"))
    monkeypatch.delenv("FAKE_HAPPIER_FAIL", raising=False)
    return Fakes(happier_state, tmp_path / "tmux.log", tmp_path / "board-state" / "board.sqlite3")


@pytest.fixture
def runner_box(
    fake_sign: "FakeSign", board_server: "BoardServer", fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> Fakes:
    for line in fake_sign.configure(box=BOX, kinds=KINDS).values():
        board_server.registry.add_line(line)
    monkeypatch.setenv("AGENT_BOARD_URL", board_server.url)
    return fakes


def _happier(fakes: Fakes) -> dict[str, Any]:
    text = fakes.happier_state.read_text()
    state: dict[str, Any] = json.loads(text) if text.strip() else {}
    return {"sessions": [], "notifications": [], **state}


def _seed_sessions(fakes: Fakes, probed_object: str, **fields: Any) -> None:
    state = _happier(fakes)
    for entry in state["sessions"]:
        entry[probed_object].update(fields)
    fakes.happier_state.write_text(json.dumps(state))


def _post(agent_board: "AgentBoard", author: str, title: str, body: str) -> None:
    posted = agent_board("--as", author, "post", "general", "--title", title, "--json", body)
    assert posted.returncode == 0, f"post as {author} failed: {posted.returncode} {posted.stderr}"


def _drain(agent_board: "AgentBoard") -> None:
    drained = agent_board("runner", "--once")
    assert drained.returncode == 0, f"runner --once failed: {drained.returncode} {drained.stderr}"


def _expire_leases(fakes: Fakes) -> None:
    with closing(sqlite3.connect(fakes.board_db)) as conn, conn:
        conn.execute("UPDATE wakes SET leased_until = 0 WHERE acked_at IS NULL")


def _redelivered(agent_board: "AgentBoard", fakes: Fakes, kind: str) -> list[str]:
    _expire_leases(fakes)
    inbox = agent_board("--as", kind, "inbox", "--json")
    assert inbox.returncode == 0, f"inbox as {kind} failed: {inbox.returncode} {inbox.stderr}"
    return [wake["summary"] for wake in json.loads(inbox.stdout)]


def _tmux_calls(fakes: Fakes) -> list[list[str]]:
    if not fakes.tmux_log.exists():
        return []
    return [json.loads(line) for line in fakes.tmux_log.read_text().splitlines()]


def _write_user_config(text: str) -> None:
    path = Path.home() / ".agents" / "board" / "runner.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_mention_wake_results_in_one_pi_session(
    fake_sign: "FakeSign",
    board_server: "BoardServer",
    fakes: Fakes,
    agent_board: "AgentBoard",
    claude_ai_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = fake_sign.configure(box=BOX, kinds=KINDS)
    for line in lines.values():
        board_server.registry.add_line(line)
    monkeypatch.setenv("AGENT_BOARD_URL", board_server.url)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    posted = agent_board("post", "general", "--title", "Review", "--json", f"@{PI} please look")
    assert posted.returncode == 0, f"post as claude failed: {posted.returncode} {posted.stderr}"
    fakes.happier_state.write_text("")
    monkeypatch.delenv("AI_AGENT")

    drained = agent_board("runner", "--once")

    assert drained.returncode == 0, f"runner --once failed: {drained.returncode} {drained.stderr}"
    state = _happier(fakes)
    assert len(state["sessions"]) == 1, f"expected one session, state={state}"
    session = state["sessions"][0]["session"]
    assert session["agentId"] == "pi", f"wrong backend spawned: {session}"
    assert session["title"] == "agent board: pi@testbox", f"wrong title: {session['title']}"


def test_runner_records_session_id_and_acks_wake(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    _post(agent_board, "claude", "Review", f"@{PI} please look")

    _drain(agent_board)

    created = [entry["session"]["id"] for entry in _happier(runner_box)["sessions"]]
    record = json.loads((Path.home() / ".agents" / "board" / "pi" / "runner-session.json").read_text())
    assert [record["session_id"]] == created, f"recorded {record}, created {created}"
    assert _redelivered(agent_board, runner_box, "pi") == [], "a dispatched wake was not acked"


def test_idle_session_gets_message_not_new_session(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    _post(agent_board, "claude", "First", f"@{PI} one")
    _drain(agent_board)
    _post(agent_board, "claude", "Second", f"@{PI} two")

    _drain(agent_board)

    sessions = _happier(runner_box)["sessions"]
    assert len(sessions) == 1, f"idle session not reused: {sessions}"
    messages = sessions[0]["messages"]
    assert len(messages) == 2 and "Second" in messages[1], f"wake not sent to the session: {messages}"


@pytest.mark.parametrize(
    "agent_state", [{"pendingRequestsCount": 1}, {"controlledByUser": True}], ids=["busy", "controlled"]
)
def test_busy_or_controlled_session_leaves_wake_leased(
    runner_box: Fakes, agent_board: "AgentBoard", agent_state: dict[str, Any]
) -> None:
    _post(agent_board, "claude", "First", f"@{PI} one")
    _drain(agent_board)
    _seed_sessions(runner_box, "agentState", **agent_state)
    _post(agent_board, "claude", "Second", f"@{PI} two")

    _drain(agent_board)

    state = _happier(runner_box)
    assert [len(entry["messages"]) for entry in state["sessions"]] == [1], f"session disturbed: {state}"
    assert state["notifications"] == [], f"a busy session is not a failure: {state['notifications']}"
    redelivered = _redelivered(agent_board, runner_box, "pi")
    assert [("Second" in summary) for summary in redelivered] == [True], f"wake not left: {redelivered}"


def test_human_wake_becomes_notification(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    _post(agent_board, "claude", "Decision", "@human may I drop the table?")

    _drain(agent_board)
    _expire_leases(runner_box)
    _drain(agent_board)

    notifications = _happier(runner_box)["notifications"]
    assert len(notifications) == 1, f"expected one notification, got {notifications}"
    assert notifications[0]["title"] == "agent board", f"wrong title: {notifications[0]}"
    assert CLAUDE in notifications[0]["message"] and "Decision" in notifications[0]["message"], (
        f"summary not relayed: {notifications[0]}"
    )
    assert _happier(runner_box)["sessions"] == [], "a human wake opened an agent session"


def test_session_home_keeps_agent_sessions_off_the_humans_happier_account(
    runner_box: Fakes, agent_board: "AgentBoard"
) -> None:
    board_home = Path.home() / ".happier-board"
    _write_user_config('session_home = "~/.happier-board"\n')
    _post(agent_board, "claude", "First", f"@{PI} one")
    _drain(agent_board)
    _post(agent_board, "claude", "Second", f"@{PI} two")
    _drain(agent_board)
    _post(agent_board, "claude", "Decision", "@human may I drop the table?")
    _drain(agent_board)

    state = _happier(runner_box)
    assert [entry["homes"] for entry in state["sessions"]] == [[str(board_home)] * 2], state["sessions"]
    assert [note["home"] for note in state["notifications"]] == [None], state["notifications"]


def test_inactive_session_is_replaced_by_a_new_one(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    _post(agent_board, "claude", "First", f"@{PI} one")
    _drain(agent_board)
    _seed_sessions(runner_box, "session", active=False)
    _post(agent_board, "claude", "Second", f"@{PI} two")

    _drain(agent_board)

    sessions = _happier(runner_box)["sessions"]
    first_prompts = [entry["messages"] for entry in sessions]
    assert len(sessions) == 2 and "Second" in first_prompts[1][0], f"no fresh session: {first_prompts}"
    assert len(first_prompts[0]) == 1, f"wake sent into the inactive session: {first_prompts[0]}"


def test_notify_human_false_leaves_human_wakes_for_another_box(
    runner_box: Fakes, agent_board: "AgentBoard"
) -> None:
    _write_user_config("notify_human = false\n")
    _post(agent_board, "claude", "Decision", "@human may I drop the table?")

    _drain(agent_board)
    silent = _happier(runner_box)["notifications"]
    _write_user_config("notify_human = true\n")
    _drain(agent_board)

    assert silent == [], f"notify_human = false still notified: {silent}"
    relayed = _happier(runner_box)["notifications"]
    assert len(relayed) == 1, f"the human wake was leased by the silent runner: {relayed}"


def test_failed_create_leaves_wake_leased_and_notifies(
    runner_box: Fakes, agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    _post(agent_board, "claude", "Review", f"@{PI} please look")
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "create:spawn_failed")

    _drain(agent_board)

    state = _happier(runner_box)
    assert state["sessions"] == [], f"no session should exist: {state}"
    messages = [notification["message"] for notification in state["notifications"]]
    assert len(messages) == 1 and PI in messages[0] and "spawn_failed" in messages[0], (
        f"failure not notified: {messages}"
    )
    assert len(_redelivered(agent_board, runner_box, "pi")) == 1, "failed wake was acked"


def test_failed_send_leaves_wake_leased_and_notifies(
    runner_box: Fakes, agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    _post(agent_board, "claude", "First", f"@{PI} one")
    _drain(agent_board)
    _post(agent_board, "claude", "Second", f"@{PI} two")
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "send:session_not_found")

    _drain(agent_board)

    state = _happier(runner_box)
    assert [len(entry["messages"]) for entry in state["sessions"]] == [1], f"session disturbed: {state}"
    messages = [notification["message"] for notification in state["notifications"]]
    assert len(messages) == 1 and "session_not_found" in messages[0], f"failure not notified: {messages}"
    redelivered = _redelivered(agent_board, runner_box, "pi")
    assert [("Second" in summary) for summary in redelivered] == [True], f"wake not left: {redelivered}"


def test_pending_wakes_become_one_message(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    _post(agent_board, "claude", "First", f"@{PI} one")
    _post(agent_board, "claude", "Second", f"@{PI} two")

    _drain(agent_board)

    sessions = _happier(runner_box)["sessions"]
    assert len(sessions) == 1, f"expected one session: {sessions}"
    messages = sessions[0]["messages"]
    assert len(messages) == 1 and "First" in messages[0] and "Second" in messages[0], (
        f"wakes not coalesced into one prompt: {messages}"
    )


@pytest.mark.parametrize(
    ("kind", "fallback", "starts_remote_control"),
    [("claude", "remote-control", True), ("claude", "none", False), ("pi", "remote-control", False)],
)
def test_remote_control_fallback_only_when_configured(
    runner_box: Fakes,
    agent_board: "AgentBoard",
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    fallback: str,
    starts_remote_control: bool,
) -> None:
    _write_user_config(f'[kind.{kind}]\nfallback = "{fallback}"\n')
    author = next(other for other in KINDS if other != kind)
    _post(agent_board, author, "Review", f"@{kind}@{BOX} please look")
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "create:spawn_failed")

    _drain(agent_board)

    name = f"board-{kind}"
    workdir = Path.home() / "agent-board-work" / kind
    started = [call for call in _tmux_calls(runner_box) if call[:1] == ["new-session"]]
    expected = [
        "new-session",
        "-d",
        "-s",
        name,
        "-c",
        str(workdir),
        "claude",
        "remote-control",
        "--name",
        name,
    ]
    assert started == ([expected] if starts_remote_control else []), f"wrong tmux calls: {started}"
    acked = _redelivered(agent_board, runner_box, kind) == []
    assert acked == starts_remote_control, f"wake acked={acked} with fallback={fallback} for {kind}"


def test_runner_signs_as_each_kind_without_ai_agent(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    _post(agent_board, "claude", "For pi", f"@{PI} one")
    _post(agent_board, "pi", "For claude", f"@{CLAUDE} two")

    _drain(agent_board)

    sessions = [entry["session"] for entry in _happier(runner_box)["sessions"]]
    spawned = sorted((session["agentId"], session["title"]) for session in sessions)
    assert spawned == [("claude", f"agent board: {CLAUDE}"), ("pi", f"agent board: {PI}")], (
        f"runner did not act as each kind: {sessions}"
    )


def test_wake_test_opens_the_session_later_wakes_reuse(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    opened = agent_board("wake-test", "pi", "--json")
    assert opened.returncode == 0, f"wake-test failed: {opened.returncode} {opened.stderr}"
    _post(agent_board, "claude", "Review", f"@{PI} please look")

    _drain(agent_board)

    sessions = _happier(runner_box)["sessions"]
    assert [entry["session"]["id"] for entry in sessions] == [json.loads(opened.stdout)["session_id"]], (
        f"wake did not reuse the wake-test session: {sessions}"
    )
    assert len(sessions[0]["messages"]) == 2, f"wake not sent after the greeting: {sessions[0]['messages']}"


def test_runner_config_merges_etc_and_home_and_rejects_home_workdir(
    fake_sign: "FakeSign", fakes: Fakes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    system = tmp_path / "agent-board-runner.toml"
    system.write_text(
        'coalesce_seconds = 5\nnotify_human = false\n[kind.pi]\nworkdir = "~/pi-work"\npermission_mode = "yolo"\n'
    )
    _write_user_config('notify_human = true\n[kind.pi]\npermission_mode = "auto"\n')

    merged = RunnerConfig.load()
    _write_user_config('[kind.pi]\nworkdir = "~"\n')

    home = Path.home()
    pi, claude = merged.kinds["pi"], merged.kinds["claude"]
    assert (merged.coalesce_seconds, merged.poll_wait, merged.notify_human) == (5, 30, True), (
        f"globals not merged: {merged}"
    )
    assert (pi.workdir, pi.permission_mode, pi.backend) == (home / "pi-work", "auto", "agent:pi"), (
        f"pi not merged: {pi}"
    )
    assert (claude.workdir, claude.permission_mode, claude.fallback) == (
        home / "agent-board-work" / "claude",
        "default",
        "none",
    ), f"claude defaults wrong: {claude}"
    assert pi.workdir.is_dir() and claude.workdir.is_dir(), "workdirs not created on load"
    with pytest.raises(ConfigError, match="workdir"):
        RunnerConfig.load()


def test_prompt_names_each_wake_and_does_not_ask_for_an_ack() -> None:
    wakes = [
        {"reason": "mention", "summary": 'claude@laptop mentioned you in thread 12 "Disk alert"'},
        {"reason": "follow", "summary": 'pi@buildbox posted in thread 9 "Backups"'},
    ]

    prompt = prompt_for(wakes)

    assert 'thread 12 "Disk alert"' in prompt and 'thread 9 "Backups"' in prompt, prompt
    assert "already acknowledged" in prompt, prompt
    assert "inbox ack" not in prompt and "Run `agent-board inbox`" not in prompt, prompt
    assert "scheduled wake" not in prompt, prompt


def test_prompt_for_a_timer_wake_points_at_the_agents_tickets() -> None:
    prompt = prompt_for([{"reason": "timer", "summary": "scheduled wake, every 21600 s"}])

    assert "every 21600 s" in prompt and "agent-board tickets list --mine" in prompt, prompt
