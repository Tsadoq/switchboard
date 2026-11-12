import re
import sqlite3
from typing import Literal

from board.constants import HUMAN
from board.forum.fanout import WakeRequest

type OriginKind = Literal["human", "budgeted"]

_POST_REF = re.compile(r"post:([1-9][0-9]*)", re.ASCII)


def classify_origin(conn: sqlite3.Connection, request: WakeRequest) -> OriginKind:
    if request.reason not in ("mention", "follow"):
        return "budgeted"
    match = _POST_REF.fullmatch(request.source_ref)
    if match is None:
        return "budgeted"
    post_id = match.group(1)
    if len(post_id) > 19 or int(post_id) > 2**63 - 1:
        return "budgeted"
    post = conn.execute(
        "SELECT author, author_kind, thread_id FROM posts WHERE id = ?", (int(post_id),)
    ).fetchone()
    if post is None or post["author"] != HUMAN or post["author_kind"] not in ("human", "system"):
        return "budgeted"
    if request.thread_id is not None and request.thread_id != post["thread_id"]:
        return "budgeted"
    return "human"
