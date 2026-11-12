import argparse
from typing import Any

from agent_board.commands.forum import PAGE_MAX, READ_LIMIT_MAX, _read_after


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
            "posts": [{"id": post, "body": str(post)} for post in page],
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
