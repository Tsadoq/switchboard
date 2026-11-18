import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import effective_queue as queue

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import CONNECT, queue_path, tables
    from packages.agent_board.tests.test_night_effective_queue_counts import TRANSITIONS, Operation, Prepare
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_effective_queue import CONNECT, queue_path, tables
    from test_night_effective_queue_counts import TRANSITIONS, Operation, Prepare
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]


def history(path: Path, size: int, start: int = 0) -> None:
    def run(number: int) -> str:
        return f"{number:08x}-0000-4000-8000-{number:012x}"

    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO receipts(receipt_id,action,run_id,payload,acked) VALUES(?,?,?,?,1)",
            (
                (run(0x90000000 + number), ("binding", "evidence", "capture")[number % 3], run(number), "{}")
                for number in range(start, start + size)
            ),
        )
        conn.executemany(
            "INSERT INTO bindings VALUES(?,1000,?,?,?,1)",
            ((run(number), run(number), "b" * 32, "/history") for number in range(start, start + size)),
        )
        conn.executemany(
            "INSERT INTO capture_aborts(parent_receipt_id,abort_receipt_id) VALUES(?,?)",
            (
                (run(0x90000000 + number), run(0xA0000000 + number))
                for number in range(start + 2, start + size, 3)
            ),
        )
        conn.execute("COMMIT")


@pytest.mark.parametrize("size", [10, 10_000, 100_000])
@pytest.mark.parametrize(
    ("count", "prepare", "operation"),
    [pytest.param(count, prepare, operation, id=name) for name, count, prepare, operation in TRANSITIONS],
)
def test_statement_counts_do_not_grow_with_history(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
    count: int,
    prepare: Prepare,
    operation: Operation,
) -> None:
    history(queue_path, size)
    control = prepare(queue_path)
    before = tables(queue_path)
    tape = Tape(monkeypatch)

    result = operation(queue_path, control)
    tape.dispose()

    assert result.fault is None and len(tape.native) == len(tape.attempts) == count
    after = tables(queue_path)
    for table in ("captures", "capture_aborts", "arms"):
        assert after[table] == before[table]
    unrelated = [row for row in before["receipts"] if row[0] <= size]
    assert [row for row in after["receipts"] if row[0] <= size] == unrelated


@pytest.mark.parametrize(
    ("query", "parameters"),
    [
        (queue.SELECTED_QUERY, (1,)),
        (queue.RECEIPT_ID_QUERY, ("50000000-0000-4000-8000-000000000000",)),
        (queue.EVIDENCE_QUERY, ("20000000-0000-4000-8000-000000000000",)),
        (queue.BINDING_QUERY, ("20000000-0000-4000-8000-000000000000",)),
        (queue.PARENT_QUERY, ("50000000-0000-4000-8000-000000000000",)),
        (queue.GREATEST_QUERY, ()),
    ],
)
def test_lookups_use_a_key_and_the_greatest_sequence_costs_the_same_at_any_history(
    queue_path: Path, query: str, parameters: tuple[object, ...]
) -> None:
    ticks = [0]

    def tick() -> int:
        ticks[0] += 1
        return 0

    steps: list[int] = []
    for total, already in ((10, 0), (10_000, 10)):
        history(queue_path, total - already, start=already)
        with closing(sqlite3.connect(queue_path)) as conn:
            plan = " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {query}", parameters))
            ticks[0] = 0
            conn.set_progress_handler(tick, 1)
            conn.execute(query, parameters).fetchall()
            steps.append(ticks[0])
        if query == queue.GREATEST_QUERY:
            assert plan == "SCAN receipts"
        else:
            assert "SEARCH" in plan and "USING" in plan, plan
    assert steps[0] == steps[1], steps
