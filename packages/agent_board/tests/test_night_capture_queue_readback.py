import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq
from agent_board.night.journal_latch import MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue_counts import TRANSITIONS
    from packages.agent_board.tests.test_night_effective_queue import CONNECT, queue_path, tables
else:
    from test_night_capture_queue_counts import TRANSITIONS
    from test_night_effective_queue import CONNECT, queue_path, tables

__all__ = ["queue_path"]
MUTATING = {name: (prepare, operation) for name, _, _, prepare, operation in TRANSITIONS if "-" in name}
MUTATING = {name: value for name, value in MUTATING.items() if not name.startswith(("observe", "identical"))}
RECEIPT = list(eq._RECEIPT)
POST_READS: dict[str, list[tuple[str, int, list[str]]]] = {
    "publish-capture": [(eq.SELECTED_QUERY, 1, RECEIPT), (cq.CAPTURES_QUERY, 2, list(cq._CAPTURES))],
    "acknowledge-capture": [(eq.SELECTED_QUERY, 2, RECEIPT), (cq.CAPTURES_QUERY, 2, list(cq._CAPTURES))],
    "publish-abort": [
        (eq.SELECTED_QUERY, 2, RECEIPT),
        (cq.LINK_BY_ABORT_QUERY, 2, list(cq._LINK)),
        (cq.RECEIPT_BY_ID_QUERY, 1, RECEIPT),
        (cq.CAPTURES_QUERY, 2, list(cq._CAPTURES)),
    ],
    "resolve-aborted": [
        (eq.SELECTED_QUERY, 2, RECEIPT),
        (cq.LINK_BY_ABORT_QUERY, 2, list(cq._LINK)),
        (cq.RECEIPT_BY_ID_QUERY, 2, RECEIPT),
        (cq.CAPTURES_QUERY, 2, list(cq._CAPTURES)),
    ],
}
POST_READS["resolve-applied"] = POST_READS["resolve-aborted"]


def changed(name: str, value: Any) -> Any:
    if isinstance(value, bytes):
        if name == "outcome":
            return b"applied" if value == b"aborted" else b"aborted"
        return (b"b" if value[:1] == b"a" else b"a") + value[1:]
    if isinstance(value, float):
        return value + 1.0
    assert isinstance(value, int)
    return value ^ 1 if name == "acked" else value + 1


NOT_NULL = {
    "snapshot": (b"{}", "text"),
    "assignment": (b"{}", "blob"),
    "outcome": (b"aborted", "text"),
    "proof": (b"{}", "text"),
}
CASES = [
    pytest.param(name, query, number, column, id=f"{name}-{label}-{column}")
    for name, reads in POST_READS.items()
    for query, number, columns in reads
    for label in [
        {
            eq.SELECTED_QUERY: "selected",
            cq.CAPTURES_QUERY: "captures",
            cq.LINK_BY_ABORT_QUERY: "link",
            cq.RECEIPT_BY_ID_QUERY: "parent",
        }[query]
    ]
    for column in columns
]


@pytest.mark.parametrize(("name", "query", "number", "column"), CASES)
def test_every_field_of_every_post_write_read_is_compared(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, query: str, number: int, column: str
) -> None:
    prepare, operation = MUTATING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    reads = [0]
    mutated: list[str] = []

    class Cursor:
        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self.cursor = cursor

        def __getattr__(self, attribute: str) -> Any:
            return getattr(self.cursor, attribute)

        def fetchall(self) -> list[Any]:
            rows = self.cursor.fetchall()
            reads[0] += 1
            if reads[0] != number or len(rows) != 1:
                return rows
            row = dict(rows[0])
            if row[column] is None:
                row[column], row[f"{column}_kind"] = NOT_NULL[column]
            else:
                row[column] = changed(column, row[column])
            mutated.append(column)
            return [row]

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> Any:
            cursor = super().execute(sql, parameters)
            return Cursor(cursor) if sql == query else cursor

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return CONNECT(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    result = operation(queue_path, control)
    monkeypatch.undo()

    assert mutated == [column]
    assert (result.fault, result.refusal) == (cq.Fault.SNAPSHOT_FAILURE, None), result
    assert result.mutation == MutationOutcome.ROLLED_BACK
    assert result.control is None and result.episode is None and result.completion is None
    assert tables(queue_path) == before
