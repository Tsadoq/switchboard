import argparse
import io
import json
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from agent_board import identity, output
from agent_board.commands import forum
from agent_board.commands.forum import PAGE_MAX, READ_LIMIT_MAX, _read_after
from agent_board.cursors import CONSUMER_ENV, hand_off
from agent_board.identity import Identity


class PagingBoard:
    """The server's thread paging: ids above the cursor, oldest first, at most `limit`, and whether more remain.

    Ids are global, so a thread's ids have gaps; `arrive` adds posts between page requests."""

    def __init__(self, ids: list[int]) -> None:
        self.ids = sorted(ids)
        self.requests: list[tuple[int, int]] = []
        self.arrive: list[int] = []

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        after, limit = params["after"], params["limit"]
        assert 1 <= limit <= PAGE_MAX
        self.requests.append((after, limit))
        if self.arrive and len(self.requests) == 2:
            self.ids = sorted(self.ids + self.arrive)
        newer = [post for post in self.ids if post > after]
        page = newer[:limit]
        return {
            "id": 31,
            "subforum": "general",
            "kind": "discussion",
            "title": "t",
            "posts": [
                {
                    "id": post,
                    "body": str(post),
                    "verified": True,
                    "author": "pi@dev",
                    "author_kind": "agent",
                    "created_at": "2026-11-12T18:00:00Z",
                    "files": [],
                }
                for post in page
            ],
            "page": {
                "total": len(self.ids),
                "older": None,
                "newer": page[-1] if len(newer) > limit else None,
            },
        }


def _read(board: PagingBoard, after: int, limit: int | None = None) -> dict[str, Any]:
    return _read_after(board, argparse.Namespace(thread_id=31, after=after, limit=limit))


def test_every_newer_post_across_pages_is_returned_once_in_order() -> None:
    ids = list(range(10, 10 + 3 * 450, 3))
    board = PagingBoard(ids)

    shown = _read(board, after=0)

    assert [post["id"] for post in shown["posts"]] == ids
    assert shown["more_after"] is None
    assert len(board.requests) == 3


def test_posts_arriving_during_traversal_are_neither_skipped_nor_repeated() -> None:
    board = PagingBoard(list(range(1, 400, 2)) + list(range(401, 500, 2)))
    board.arrive = [500, 502]

    shown = [post["id"] for post in _read(board, after=0)["posts"]]

    assert len(shown) == len(set(shown)), "a post was shown twice"
    assert shown == sorted(shown)
    assert shown[-2:] == [500, 502]


def test_the_limit_caps_the_output_and_names_the_continuation() -> None:
    board = PagingBoard(list(range(1, 2000)))

    shown = _read(board, after=0, limit=250)

    assert [post["id"] for post in shown["posts"]] == list(range(1, 251))
    assert shown["more_after"] == 250
    assert board.requests == [(0, 200), (200, 50)]


def test_without_a_limit_the_output_stops_at_the_cap_and_says_so() -> None:
    board = PagingBoard(list(range(1, 3000)))

    shown = _read(board, after=0)

    assert len(shown["posts"]) == READ_LIMIT_MAX
    assert shown["more_after"] == READ_LIMIT_MAX


def test_an_empty_page_ends_the_read_without_a_continuation() -> None:
    board = PagingBoard([5, 6])

    shown = _read(board, after=6)

    assert shown["posts"] == [] and shown["more_after"] is None


class StuckBoard(PagingBoard):
    def get(self, path: str, **params: Any) -> dict[str, Any]:
        page = super().get(path, **params)
        page["posts"] = [{"id": 1, "body": "again"}]
        return page


def test_a_page_that_does_not_advance_stops_the_read_instead_of_repeating_posts() -> None:
    board = StuckBoard(list(range(1, 500)))

    with pytest.raises(SystemExit, match="does not advance"):
        _read(board, after=5)


class Session:
    """`agent-board read --new` in process, against the paging fake, with its own cursor directory."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board: PagingBoard) -> None:
        self.state = tmp_path / "state"
        self.state.mkdir(mode=0o700)
        self.board = board
        monkeypatch.setattr(identity, "resolve", lambda kind: Identity("claude@dev", "claude", "dev"))
        monkeypatch.setattr(identity, "ensure_state_dir", lambda kind: self.state)
        monkeypatch.setattr(forum, "board_client", lambda args, me=None: nullcontext(self.board))
        monkeypatch.setenv(CONSUMER_ENV, "c" * 32)

    def read(self, *arguments: str) -> tuple[int, str]:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command")
        forum.register(commands)
        args = parser.parse_args(["read", "31", *arguments])
        args.as_kind, args.board, args.json = None, None, False
        with redirect_stdout(io.StringIO()) as out:
            try:
                code = args.handler(args)
            except SystemExit as stop:
                return 1, str(stop)
        return code, out.getvalue()

    def cursor(self) -> int | None:
        path = self.state / "cursors" / ("c" * 32) / "31.json"
        return json.loads(path.read_text())["post"] if path.exists() else None


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    return Session(tmp_path, monkeypatch, PagingBoard(list(range(1, 11))))


def _shown(text: str) -> list[int]:
    return [int(line.split()[2]) for line in text.splitlines() if line.startswith("--- post ")]


def test_new_needs_a_starting_point_the_first_time_then_shows_only_what_is_unprinted(
    session: Session,
) -> None:
    code, text = session.read("--new")
    assert code == 1 and "no read cursor yet" in text

    code, text = session.read("--new", "--start-after", "6")
    assert code == 0 and _shown(text) == [7, 8, 9, 10] and session.cursor() == 10

    session.board.ids += [11, 12]
    assert _shown(session.read("--new")[1]) == [11, 12] and session.cursor() == 12
    assert _shown(session.read("--new")[1]) == []


def test_a_trigger_older_than_the_cursor_is_shown_again_and_marked(session: Session) -> None:
    session.read("--new", "--start-after", "8")

    text = session.read("--new", "--start-after", "4")[1]

    assert _shown(text) == [5, 6, 7, 8, 9, 10]
    assert text.count("at or behind your read cursor") == 6, "a replayed post was presented as new"
    assert "printed before" not in text and "shown before" not in text, (
        "the label claims more than the cursor knows"
    )
    assert session.cursor() == 10


def test_a_failed_write_to_stdout_leaves_the_cursor_where_it_was(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.read("--new", "--start-after", "4")
    session.board.ids += [11]

    def broken(*args: Any) -> None:
        raise BrokenPipeError("reader went away")

    monkeypatch.setattr(output, "emit_text", broken)

    assert session.read("--new")[0] == 1
    assert session.cursor() == 10, "the cursor advanced over posts that were never printed"


def test_a_replay_cut_by_the_limit_continues_statelessly_without_lowering_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(tmp_path, monkeypatch, PagingBoard(list(range(1, 301))))
    session.read("--new", "--start-after", "250")
    seen: list[int] = []

    text = session.read("--new", "--start-after", "10", "--limit", "100")[1]
    seen += _shown(text)
    while "more posts follow:" in text:
        after = text.rsplit("--after ", 1)[1].split()[0]
        text = session.read("--after", after, "--limit", "100")[1]
        seen += _shown(text)

    assert seen == list(range(11, 301)), "the replay looped or skipped"
    assert session.cursor() == 300
    session.board.ids += [301]
    assert _shown(session.read("--new")[1]) == [301]


def test_recovery_refuses_over_a_valid_cursor_and_replaces_only_a_corrupt_one(session: Session) -> None:
    session.read("--new", "--start-after", "4")
    code, text = session.read("--new", "--recover-after", "2")
    assert code == 1 and "valid" in text

    path = session.state / "cursors" / ("c" * 32) / "31.json"
    path.write_text("garbage")
    code, text = session.read("--new")
    assert code == 1 and "--recover-after" in text

    code, text = session.read("--new", "--recover-after", "8")
    assert code == 0 and _shown(text) == [9, 10] and session.cursor() == 10
    assert [name for name in path.parent.iterdir() if ".corrupt-" in name.name]


def test_a_retired_session_reads_no_more(session: Session) -> None:
    session.read("--new", "--start-after", "4")
    hand_off(session.state / "cursors", "c" * 32, "d" * 32)

    code, text = session.read("--new")

    assert code == 1 and "handed over" in text


def test_without_a_session_id_new_refuses(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONSUMER_ENV)

    code, text = session.read("--new", "--start-after", "4")

    assert code == 1 and CONSUMER_ENV in text


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--start-after", "3"], id="start-after-without-new"),
        pytest.param(["--new", "--after", "3"], id="new-with-after"),
    ],
)
def test_cursor_options_are_refused_out_of_place(session: Session, arguments: list[str]) -> None:
    assert session.read(*arguments)[0] == 1
