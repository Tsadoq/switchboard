import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.config import ConfigError
from agent_board.runner.config import RunnerConfig
from agent_board.runner.happier import Happier, HappierError
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


def _set_turn_running(fakes: Fakes, session_id: str, running: bool) -> None:
    state = _happier(fakes)
    for entry in state["sessions"]:
        if entry["session"]["id"] == session_id:
            entry["turnRunning"] = running
    fakes.happier_state.write_text(json.dumps(state))


def _open_session(happier: Happier, tmp_path: Path) -> str:
    return happier.create(
        backend="agent:pi", path=tmp_path, tag="t", title="T", prompt="hello", permission_mode="default"
    ).id


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
    record = json.loads((Path.home() / ".agents" / "board" / "pi" / "runner-sessions.json").read_text())
    recorded = [(entry["key"], entry["session_id"]) for entry in record["sessions"]]
    assert recorded == [("kind", session_id) for session_id in created], (
        f"recorded {record}, created {created}"
    )
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


def test_mentions_run_on_the_mention_model_in_the_one_session(
    runner_box: Fakes, agent_board: "AgentBoard"
) -> None:
    _write_user_config('[kind.pi.models]\ndefault = "small-model"\nmention = "big-model"\n')
    _post(agent_board, "claude", "Review", f"@{PI} please look")
    _drain(agent_board)
    _post(agent_board, "claude", "Again", f"@{PI} and this one")
    _drain(agent_board)

    sessions = _happier(runner_box)["sessions"]
    assert len(sessions) == 1, f"a model change opened another session: {sessions}"
    assert sessions[0]["models"] == ["big-model", "big-model"], sessions[0]


def test_a_due_weekly_review_fires_once_on_its_model(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    _write_user_config(
        '[kind.pi]\nweekly = "Mon 06:00 Europe/Rome"\n[kind.pi.models]\ndefault = "small-model"\nweekly = "big-model"\n'
    )
    state_dir = Path.home() / ".agents" / "board" / "pi"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "runner-weekly.json").write_text(json.dumps({"last_fired_at": 0}))

    _drain(agent_board)
    _drain(agent_board)

    sessions = _happier(runner_box)["sessions"]
    assert len(sessions) == 1 and sessions[0]["models"] == ["big-model"], sessions
    assert "weekly review" in sessions[0]["messages"][0], sessions[0]["messages"]
    fired = json.loads((state_dir / "runner-weekly.json").read_text())["last_fired_at"]
    assert fired > 0, "the delivered weekly review was not recorded"


def test_thread_scope_gives_each_thread_its_own_session(runner_box: Fakes, agent_board: "AgentBoard") -> None:
    _write_user_config('[kind.pi]\nsession_scope = "thread"\n')
    _post(agent_board, "claude", "First", f"@{PI} one")
    _post(agent_board, "claude", "Second", f"@{PI} two")

    _drain(agent_board)

    prompts = [entry["messages"] for entry in _happier(runner_box)["sessions"]]
    assert len(prompts) == 2, f"expected one session per thread: {prompts}"
    assert "First" in prompts[0][0] and "Second" not in prompts[0][0], prompts
    assert "Second" in prompts[1][0] and "First" not in prompts[1][0], prompts
    assert _redelivered(agent_board, runner_box, "pi") == [], "a delivered wake was not acked"


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
    assert re.search(r"\nhttps?://\S+/t/\d+$", notifications[0]["message"]), (
        f"the push does not link to the thread: {notifications[0]}"
    )
    assert _happier(runner_box)["sessions"] == [], "a human wake opened an agent session"


def test_session_home_keeps_agent_sessions_off_the_humans_happier_account(
    runner_box: Fakes, agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    human_home = str(Path.home() / ".happier")
    monkeypatch.setenv("HAPPIER_HOME_DIR", human_home)
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
    assert [note["home"] for note in state["notifications"]] == [human_home], state["notifications"]


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


def test_prompt_names_a_burst_from_one_author_once_with_a_count() -> None:
    burst = {"reason": "follow", "summary": 'pi@buildbox posted in thread 9 "Backups"', "thread_id": 9}
    wakes = [{**burst, "source_ref": f"post:{post}"} for post in (41, 42, 44)]
    wakes.append({"reason": "mention", "summary": 'pi@buildbox mentioned you in thread 9 "Backups"'})
    wakes.append({"reason": "follow", "summary": 'claude@laptop posted in thread 12 "Disk alert"'})

    prompt = prompt_for(wakes)

    assert prompt.startswith(
        'You were woken by the agent board: pi@buildbox posted in thread 9 "Backups" (3 posts); '
        'pi@buildbox mentioned you in thread 9 "Backups"; claude@laptop posted in thread 12 "Disk alert". '
    ), prompt
    assert "`agent-board read 9 --new --start-after 40`" in prompt, prompt


def test_prompt_lets_an_informational_wake_end_without_a_reply() -> None:
    prompt = prompt_for([{"reason": "follow", "summary": 'pi@buildbox posted in thread 9 "Backups"'}])

    assert "agent-board read <id>" in prompt, prompt
    assert "answer there" not in prompt, prompt
    assert "only when" in prompt and "needs no reply" in prompt, prompt


def test_prompt_always_requires_acknowledging_a_human_response() -> None:
    prompt = prompt_for(
        [
            {"reason": "follow", "summary": 'pi@buildbox posted in thread 9 "Backups"'},
            {"reason": "follow", "summary": 'human posted in thread 12 "Decision"'},
        ]
    )

    assert "Always reply to a post by the human that responds to you" in prompt, prompt
    assert "even if only to confirm" in prompt, prompt
    assert "a post that only informs needs no reply" in prompt, prompt


def test_prompt_for_a_timer_wake_points_at_the_agents_tickets() -> None:
    prompt = prompt_for([{"reason": "timer", "summary": "scheduled wake, every 21600 s"}])

    assert "every 21600 s" in prompt and "agent-board tickets list --mine" in prompt, prompt


@pytest.mark.parametrize(
    "text",
    [
        'kinds = ["pi"]\n[kind.pi.models]\ndefault = "d"\nsometimes = "x"\n',
        'kinds = ["pi"]\n[kind.pi.models]\ndefault = "d"\nmention = ""\n',
        'kinds = ["pi"]\n[kind.pi.models]\nmention = "m"\n',
        'kinds = ["pi"]\n[kind.pi]\nweekly = "someday"\n',
    ],
)
def test_bad_model_or_weekly_settings_are_refused(fake_sign: "FakeSign", text: str) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    _write_user_config(text)

    with pytest.raises(ConfigError):
        RunnerConfig.load()


def test_models_and_weekly_are_read_per_kind(fake_sign: "FakeSign") -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    _write_user_config(
        '[kind.pi]\nweekly = "Sun 21:30 UTC"\n[kind.pi.models]\ndefault = "d"\nmention = "m"\n'
    )

    pi = RunnerConfig.load().kinds["pi"]

    assert dict(pi.models) == {"default": "d", "mention": "m"}
    assert pi.weekly is not None and (pi.weekly.weekday, pi.weekly.hour, pi.weekly.minute) == (6, 21, 30)


def test_session_settings_default_to_one_session_per_kind(fake_sign: "FakeSign") -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    _write_user_config("")

    claude = RunnerConfig.load().kinds["claude"]

    assert (claude.session_scope, claude.max_sessions, claude.idle_ttl_seconds) == ("kind", 2, 1800.0)


def test_session_settings_are_read_per_kind(fake_sign: "FakeSign") -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    _write_user_config('[kind.claude]\nsession_scope = "thread"\nmax_sessions = 3\nidle_ttl_seconds = 600\n')

    kinds = RunnerConfig.load().kinds
    claude, pi = kinds["claude"], kinds["pi"]

    assert (claude.session_scope, claude.max_sessions, claude.idle_ttl_seconds) == ("thread", 3, 600.0)
    assert (pi.session_scope, pi.max_sessions, pi.idle_ttl_seconds) == ("kind", 2, 1800.0)


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ('session_scope = "topic"', "session_scope"),
        ("max_sessions = 0", "max_sessions"),
        ("max_sessions = true", "max_sessions"),
        ("idle_ttl_seconds = 10", "idle_ttl_seconds"),
    ],
)
def test_bad_session_settings_are_refused(fake_sign: "FakeSign", line: str, key: str) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    _write_user_config(f"[kind.claude]\n{line}\n")

    with pytest.raises(ConfigError, match=re.escape(f"kind.claude.{key} must")):
        RunnerConfig.load()


def test_archive_ends_a_session_and_tolerates_gone_ones(runner_box: Fakes, tmp_path: Path) -> None:
    happier = Happier()
    session_id = _open_session(happier, tmp_path)

    happier.archive(session_id)

    with pytest.raises(HappierError) as refused:
        happier.send(session_id, "after archive")
    assert refused.value.code == "session_archived", f"send to an archived session: {refused.value.code}"
    happier.archive(session_id)
    happier.archive("cm-unknown")


def test_idle_reflects_a_running_turn(runner_box: Fakes, tmp_path: Path) -> None:
    happier = Happier()
    session_id = _open_session(happier, tmp_path)

    _set_turn_running(runner_box, session_id, True)
    assert happier.idle(session_id) is False, "a running turn read as idle"
    _set_turn_running(runner_box, session_id, False)
    assert happier.idle(session_id) is True, "a finished turn read as busy"


@pytest.mark.parametrize(
    ("code", "idle"),
    [("timeout", False), ("session_not_found", True), ("session_archived", True)],
)
def test_idle_maps_wait_errors(
    runner_box: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, idle: bool
) -> None:
    happier = Happier()
    session_id = _open_session(happier, tmp_path)
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", f"wait:{code}")

    assert happier.idle(session_id) is idle, f"wait error {code} mapped wrongly"


def test_idle_raises_on_an_unexpected_wait_error(
    runner_box: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    happier = Happier()
    session_id = _open_session(happier, tmp_path)
    monkeypatch.setenv("FAKE_HAPPIER_FAIL", "wait:boom")

    with pytest.raises(HappierError) as raised:
        happier.idle(session_id)
    assert raised.value.code == "boom", f"unexpected wait error rewritten: {raised.value.code}"


def test_prompt_names_the_first_triggering_post_per_thread_with_an_inclusive_boundary() -> None:
    wakes = [
        {"reason": "follow", "summary": "pi posted in thread 31", "source_ref": "post:1361", "thread_id": 31},
        {
            "reason": "mention",
            "summary": "pi mentioned you in thread 31",
            "source_ref": "post:1359",
            "thread_id": 31,
        },
        {"reason": "follow", "summary": "pi posted in thread 54", "source_ref": "post:1367", "thread_id": 54},
    ]

    prompt = prompt_for(wakes)

    assert "`agent-board read 31 --new --start-after 1358`" in prompt, prompt
    assert "`agent-board read 54 --new --start-after 1366`" in prompt, prompt
    assert "--start-after 1360" not in prompt and "--start-after 1359" not in prompt, prompt
    assert "If `--new` refuses, use `--after` with the same post number" in prompt, prompt


@pytest.mark.parametrize(
    "wake",
    [
        pytest.param({"reason": "timer", "summary": "scheduled wake, every 21600 s"}, id="timer"),
        pytest.param(
            {"reason": "follow", "summary": "x", "source_ref": "ticket:4", "thread_id": 16}, id="not-a-post"
        ),
        pytest.param(
            {"reason": "follow", "summary": "x", "source_ref": "post:7", "thread_id": None}, id="no-thread"
        ),
        pytest.param(
            {"reason": "follow", "summary": "x", "source_ref": "post:07", "thread_id": 3}, id="odd-ref"
        ),
    ],
)
def test_prompt_adds_no_read_boundary_without_a_named_post(wake: dict[str, Any]) -> None:
    assert "--after" not in prompt_for([wake])


def test_a_wake_that_names_posts_leads_with_the_delta_read_not_the_full_one() -> None:
    prompt = prompt_for(
        [
            {
                "reason": "mention",
                "summary": "pi mentioned you in thread 31",
                "source_ref": "post:1400",
                "thread_id": 31,
            }
        ]
    )

    assert "Read each thread named above with `agent-board read <id>`" not in prompt, prompt
    assert prompt.index("--start-after 1399") < prompt.index("agent-board read <id>"), (
        "the full read came first"
    )
    assert "Always reply to a post by the human that responds to you" in prompt, prompt


def test_a_session_without_a_read_cursor_is_offered_only_the_stateless_read() -> None:
    wakes = [
        {
            "reason": "mention",
            "summary": "pi mentioned you in thread 52",
            "source_ref": "post:1581",
            "thread_id": 52,
        }
    ]

    legacy = prompt_for(wakes, cursor=False)
    current = prompt_for(wakes, cursor=True)

    assert "`agent-board read 52 --after 1580`" in legacy and "--new" not in legacy, legacy
    assert "`agent-board read 52 --new --start-after 1580`" in current and "If `--new` refuses" in current, (
        current
    )
