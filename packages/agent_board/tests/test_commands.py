import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.commands import STDIN_WAIT_SECONDS

if TYPE_CHECKING:
    from .conftest import AgentBoard, BoardServer, FakeSign

BOX = "testbox"
KINDS = ["claude", "pi"]
CLAUDE = f"claude@{BOX}"
PI = f"pi@{BOX}"
AGENT_BOARD = Path(sys.executable).parent / "agent-board"


def _parsed(result: subprocess.CompletedProcess[str], command: str) -> Any:
    assert result.returncode == 0, f"{command} failed: {result.returncode} {result.stderr}"
    return json.loads(result.stdout)


def _state_dir(kind: str) -> Path:
    return Path.home() / ".agents" / "board" / kind


@pytest.fixture
def claude_and_pi_on_board(
    fake_sign: "FakeSign", board_server: "BoardServer", claude_ai_agent: str, monkeypatch: pytest.MonkeyPatch
) -> "BoardServer":
    lines = fake_sign.configure(box=BOX, kinds=KINDS)
    for line in lines.values():
        board_server.registry.add_line(line)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    monkeypatch.setenv("AGENT_BOARD_URL", board_server.url)
    return board_server


def test_post_command_is_accepted_and_readable(
    fake_sign: "FakeSign",
    agent_board: "AgentBoard",
    board_server: "BoardServer",
    claude_ai_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    board_server.registry.add_line(lines["claude"])
    monkeypatch.setenv("AGENT_BOARD_URL", board_server.url)

    post = agent_board("post", "general", "--title", "Hello board", "--json", "the first signed post")
    assert post.returncode == 0, f"post failed: {post.returncode} {post.stderr}"
    thread_id = json.loads(post.stdout).get("thread_id")
    assert isinstance(thread_id, int), f"no thread id in output: {post.stdout}"
    read = agent_board("read", str(thread_id))

    assert "verified" in read.stdout, f"post not shown as verified: {read.stdout}"


def test_boards_lists_seeded_subforums(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard"
) -> None:
    boards = agent_board("boards")

    assert boards.returncode == 0, f"boards failed: {boards.returncode} {boards.stderr}"
    header, *rows = boards.stdout.splitlines()
    slugs = {row.split()[0] for row in rows}
    assert header.split()[0] == "slug", f"no table header: {boards.stdout}"
    assert {"general", "introductions", "tickets"} <= slugs, f"seeded subforums missing: {boards.stdout}"


def test_reply_appends_to_thread(claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard") -> None:
    opened = _parsed(agent_board("post", "general", "--title", "Plan", "--json", "opening"), "post")

    reply = agent_board("reply", str(opened["thread_id"]), "--json", stdin="a reply from stdin\n")

    replied = _parsed(reply, "reply")
    thread = _parsed(agent_board("read", str(opened["thread_id"]), "--json"), "read")
    bodies = [post["body"] for post in thread["posts"]]
    assert replied["thread_id"] == opened["thread_id"], f"reply opened another thread: {replied}"
    assert bodies == ["opening", "a reply from stdin\n"], f"reply not appended: {bodies}"


def test_post_reads_body_from_file(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", tmp_path: Path
) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("# Findings\n\nThe sweeper is fine.\n")

    post = agent_board("post", "general", "--title", "Findings", "--file", str(draft), "--json")

    thread = _parsed(agent_board("read", str(_parsed(post, "post")["thread_id"]), "--json"), "read")
    assert thread["posts"][0]["body"] == draft.read_text(), f"body not taken from --file: {thread['posts']}"


def test_follow_then_threads_since(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AI_AGENT")
    _parsed(agent_board("--as", "claude", "post", "general", "--title", "Old", "--json", "before"), "post")
    follow = _parsed(agent_board("--as", "claude", "follow", "subforum:general", "--json"), "follow")
    since = datetime.now(UTC).isoformat()
    _parsed(agent_board("--as", "pi", "post", "general", "--title", "New", "--json", "after"), "post")

    threads = agent_board("--as", "claude", "threads", "general", "--since", since, "--json")
    inbox = agent_board("--as", "claude", "inbox", "--json")

    assert follow == {"target": "subforum:general", "following": True}, f"follow not recorded: {follow}"
    titles = [thread["title"] for thread in _parsed(threads, "threads --since")]
    assert titles == ["New"], f"--since did not filter to newer threads: {titles}"
    reasons = [wake["reason"] for wake in _parsed(inbox, "inbox")]
    assert reasons == ["follow"], f"following the subforum woke nobody: {reasons}"


def test_unfollow_stops_follow_wakes(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AI_AGENT")
    _parsed(agent_board("--as", "claude", "follow", "subforum:general", "--json"), "follow")

    unfollow = agent_board("--as", "claude", "unfollow", "subforum:general", "--json")
    _parsed(
        agent_board("--as", "pi", "post", "general", "--title", "Quiet", "--json", "nobody listens"), "post"
    )

    inbox = _parsed(agent_board("--as", "claude", "inbox", "--json"), "inbox")
    assert _parsed(unfollow, "unfollow") == {"target": "subforum:general", "following": False}, (
        f"unfollow not recorded: {unfollow.stdout}"
    )
    assert inbox == [], f"unfollowed subforum still wakes: {inbox}"


def test_introduce_sets_profile_visible_in_agents(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard"
) -> None:
    introduce = agent_board(
        "introduce", "--name", "Claudia", "--speciality", "python", "sqlite", "--wake-every", "6h", "--json"
    )

    introduced = _parsed(introduce, "introduce")
    agents = {agent["principal"]: agent for agent in _parsed(agent_board("agents", "--json"), "agents")}
    profile = {key: agents[CLAUDE][key] for key in ("display_name", "specialities", "wake_every_seconds")}
    assert introduced["introduced"] is True, f"first introduction not announced: {introduced}"
    assert profile == {
        "display_name": "Claudia",
        "specialities": ["python", "sqlite"],
        "wake_every_seconds": 6 * 3600,
    }, f"profile not visible: {agents[CLAUDE]}"


def test_tickets_create_then_comment_lands_in_ticket_thread(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard"
) -> None:
    ticket = _parsed(agent_board("tickets", "create", "--title", "Fix the sweeper", "--json"), "create")

    comment = agent_board("tickets", "comment", str(ticket["id"]), "--json", "started on it")

    commented = _parsed(comment, "tickets comment")
    thread = _parsed(agent_board("read", str(ticket["thread_id"]), "--json"), "read")
    last = thread["posts"][-1]
    assert commented["thread_id"] == ticket["thread_id"], f"comment missed the ticket thread: {commented}"
    assert (last["body"], last["author"], last["verified"]) == ("started on it", CLAUDE, True), (
        f"comment not in ticket thread: {thread['posts']}"
    )


def test_tickets_move_and_release_change_column_and_assignee(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard"
) -> None:
    ticket_id = str(_parsed(agent_board("tickets", "create", "--title", "Ship", "--json"), "create")["id"])
    _parsed(agent_board("tickets", "claim", ticket_id, "--json"), "claim")

    moved = _parsed(agent_board("tickets", "move", ticket_id, "doing", "--json"), "tickets move")
    released = _parsed(agent_board("tickets", "release", ticket_id, "--json"), "tickets release")

    assert (moved["column"], moved["assignee"]) == ("doing", CLAUDE), f"move not applied: {moved}"
    assert (released["column"], released["assignee"]) == ("doing", None), f"release not applied: {released}"


def test_tickets_list_mine_filters_by_assignee(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AI_AGENT")
    ids = [
        _parsed(agent_board("--as", kind, "tickets", "create", "--title", title, "--json"), "create")["id"]
        for kind, title in (("claude", "Mine"), ("claude", "Nobody's"), ("pi", "Theirs"))
    ]
    _parsed(agent_board("--as", "claude", "tickets", "claim", str(ids[0]), "--json"), "claim")
    _parsed(agent_board("--as", "pi", "tickets", "claim", str(ids[2]), "--json"), "claim")

    mine = agent_board("--as", "claude", "tickets", "list", "--mine", "--json")

    assert [ticket["id"] for ticket in _parsed(mine, "tickets list --mine")] == [ids[0]], (
        f"--mine did not filter by assignee: {mine.stdout}"
    )


def test_tickets_claim_conflict_exits_1_with_reason(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AI_AGENT")
    created = agent_board("--as", "claude", "tickets", "create", "--title", "Contested", "--json")
    ticket_id = str(_parsed(created, "create")["id"])
    _parsed(agent_board("--as", "claude", "tickets", "claim", ticket_id, "--json"), "claim")

    second = agent_board("--as", "pi", "tickets", "claim", ticket_id)

    assert second.returncode == 1, f"conflicting claim not exit 1: {second.returncode} {second.stderr}"
    assert "already_claimed" in second.stderr, f"reason missing: {second.stderr}"
    assert CLAUDE in second.stderr, f"current assignee missing: {second.stderr}"


def test_files_put_and_get_verify_sha256(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", tmp_path: Path
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"line one\nline two\n")

    put = _parsed(agent_board("files", "put", str(source), "--json"), "files put")
    copy = agent_board("files", "get", put["id"], "-o", str(tmp_path / "copy.txt"))
    blob = next(tmp_path.glob(f"board-state/**/{put['id']}"))
    blob.write_bytes(b"LINE one\nline two\n")
    tampered = agent_board("files", "get", put["id"], "-o", str(tmp_path / "tampered.txt"))

    assert put["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest(), f"wrong digest: {put}"
    assert copy.returncode == 0, f"files get failed: {copy.returncode} {copy.stderr}"
    assert (tmp_path / "copy.txt").read_bytes() == source.read_bytes(), "downloaded bytes differ"
    assert tampered.returncode == 1, f"tampered download not exit 1: {tampered.returncode} {tampered.stderr}"
    assert "sha256" in tampered.stderr, f"mismatch not reported: {tampered.stderr}"
    assert not (tmp_path / "tampered.txt").exists(), "tampered bytes were written"


def test_files_list_shows_live_files(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", tmp_path: Path
) -> None:
    uploaded = {}
    for name, ttl_days in (("a.txt", "1"), ("b.bin", "3")):
        (tmp_path / name).write_bytes(name.encode())
        put = agent_board("files", "put", str(tmp_path / name), "--ttl-days", ttl_days, "--json")
        uploaded[_parsed(put, "files put")["id"]] = name

    listed = _parsed(agent_board("files", "list", "--json"), "files list")

    shown = {entry["id"]: entry["name"] for entry in listed}
    assert shown == uploaded, f"live files not listed: {listed}"


def test_memory_add_appends_dated_line_under_kind_dir(
    fake_sign: "FakeSign", agent_board: "AgentBoard", claude_ai_agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    before = datetime.now(UTC).date().isoformat()

    first = agent_board("memory", "add", "prefers", "uv", "over", "pip")
    second = agent_board("memory", "add", "the sweeper runs hourly")

    after = datetime.now(UTC).date().isoformat()
    assert (first.returncode, second.returncode) == (0, 0), (
        f"memory add failed: {first.stderr} {second.stderr}"
    )
    lines = (_state_dir("claude") / "memory.md").read_text().splitlines()
    expected = [
        [f"- {day}: prefers uv over pip", f"- {day}: the sweeper runs hourly"] for day in (before, after)
    ]
    assert lines in expected, f"not dated lines in claude's memory: {lines}"
    assert not _state_dir("pi").exists(), "memory add touched another kind's directory"


def test_memory_show_tail_and_path(
    fake_sign: "FakeSign", agent_board: "AgentBoard", claude_ai_agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sign.configure(box=BOX, kinds=KINDS)
    monkeypatch.setenv("AI_AGENT", claude_ai_agent)
    for note in ("one", "two", "three"):
        assert agent_board("memory", "add", note).returncode == 0, f"memory add {note} failed"

    tail = agent_board("memory", "show", "--tail", "2")
    path = agent_board("memory", "path")

    assert tail.returncode == 0, f"memory show failed: {tail.returncode} {tail.stderr}"
    assert [line.rsplit(": ", 1)[1] for line in tail.stdout.splitlines()] == ["two", "three"], (
        f"--tail 2 did not show the last two notes: {tail.stdout}"
    )
    assert path.stdout.strip() == str(_state_dir("claude") / "memory.md"), f"wrong path: {path.stdout}"


def test_inbox_lists_then_acks(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AI_AGENT")
    mention = agent_board("--as", "pi", "post", "general", "--title", "Ping", "--json", f"@{CLAUDE} look")
    _parsed(mention, "post")

    listed = _parsed(agent_board("--as", "claude", "inbox", "--json"), "inbox")
    acked = _parsed(agent_board("--as", "claude", "inbox", "ack", "--all", "--json"), "inbox ack --all")
    emptied = _parsed(agent_board("--as", "claude", "inbox", "--json"), "inbox after ack")

    assert [(wake["reason"], PI in wake["summary"]) for wake in listed] == [("mention", True)], (
        f"mention not in inbox: {listed}"
    )
    assert acked == {"acked": 1}, f"listed wake not acked: {acked}"
    assert emptied == [], f"acked wake still listed: {emptied}"


def test_inbox_ack_by_id_keeps_the_other_wakes_listed(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AI_AGENT")
    for title in ("First", "Second"):
        mention = agent_board("--as", "pi", "post", "general", "--title", title, "--json", f"@{CLAUDE} hi")
        _parsed(mention, "post")
    first, second = [
        wake["id"] for wake in _parsed(agent_board("--as", "claude", "inbox", "--json"), "inbox")
    ]

    acked = _parsed(agent_board("--as", "claude", "inbox", "ack", str(first), "--json"), "inbox ack ID")

    remaining = _parsed(agent_board("--as", "claude", "inbox", "--json"), "inbox after ack")
    assert acked == {"acked": 1}, f"wake {first} not acked: {acked}"
    assert [wake["id"] for wake in remaining] == [second], f"wrong wakes still listed: {remaining}"


def test_reply_without_body_on_an_open_silent_pipe_fails_fast(
    claude_and_pi_on_board: "BoardServer",
) -> None:
    opened = subprocess.run(
        [str(AGENT_BOARD), "post", "general", "--title", "Plan", "--json", "opening"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    thread_id = str(_parsed(opened, "post")["thread_id"])
    reply = subprocess.Popen(
        [str(AGENT_BOARD), "reply", thread_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        reply.wait(timeout=15)
    except subprocess.TimeoutExpired:
        reply.kill()
        reply.wait()
        pytest.fail("reply hung on an open stdin pipe")
    finally:
        assert reply.stdin is not None and reply.stderr is not None
        reply.stdin.close()
    stderr = reply.stderr.read()
    assert reply.returncode == 1, f"expected exit 1: {reply.returncode} {stderr}"
    assert "no body" in stderr, f"no reason given: {stderr}"


@pytest.mark.parametrize("body", ["", "  \n\t"])
def test_blank_body_is_refused_before_signing(
    claude_and_pi_on_board: "BoardServer", agent_board: "AgentBoard", body: str
) -> None:
    opened = _parsed(agent_board("post", "general", "--title", "Plan", "--json", "opening"), "post")

    reply = agent_board("reply", str(opened["thread_id"]), stdin=body)

    assert reply.returncode == 1, f"blank body accepted: {reply.returncode} {reply.stdout}"
    assert "empty" in reply.stderr, f"no reason given: {reply.stderr}"


def test_dash_body_waits_for_a_slow_stdin_writer(claude_and_pi_on_board: "BoardServer") -> None:
    opened = subprocess.run(
        [str(AGENT_BOARD), "post", "general", "--title", "Plan", "--json", "opening"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    thread_id = str(_parsed(opened, "post")["thread_id"])
    reply = subprocess.Popen(
        [str(AGENT_BOARD), "reply", thread_id, "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(STDIN_WAIT_SECONDS + 1)
    _, stderr = reply.communicate("late but valid", timeout=30)
    assert reply.returncode == 0, f"slow stdin refused: {reply.returncode} {stderr}"
    thread = subprocess.run(
        [str(AGENT_BOARD), "read", thread_id], capture_output=True, text=True, timeout=60, check=False
    )
    assert "late but valid" in thread.stdout, f"stdin body not posted: {thread.stdout}"
