import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import capture_queue as cq

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue_counts import TRANSITIONS, Operation, Prepare
    from packages.agent_board.tests.test_night_effective_queue import CONNECT, queue_path, tables
    from packages.agent_board.tests.test_night_effective_queue_scale import history
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_capture_queue_counts import TRANSITIONS, Operation, Prepare
    from test_night_effective_queue import CONNECT, queue_path, tables
    from test_night_effective_queue_scale import history
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]


def uuid(number: int) -> str:
    return f"{number:08x}-0000-4000-8000-{number:012x}"


def hidden_parents(path: Path, size: int) -> None:
    """Captures that were aborted: an ack0 parent left behind a resolved link, with its context row."""
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        for number in range(size):
            run, parent, abort = (
                uuid(0xB0000000 + number),
                uuid(0xC0000000 + number),
                uuid(0xD0000000 + number),
            )
            conn.execute(
                "INSERT INTO receipts(receipt_id,action,run_id,payload,acked) VALUES(?,'capture',?,'{}',0)",
                (parent, run),
            )
            conn.execute(
                "INSERT INTO receipts(receipt_id,action,run_id,payload,acked) VALUES(?,'capture_abort',?,'{}',1)",
                (abort, run),
            )
            conn.execute(
                "INSERT INTO capture_aborts VALUES(?,?,'aborted','{}')",
                (parent, abort),
            )
            conn.execute(
                "INSERT INTO captures VALUES(?,?,'pi@dev',2000.0,'{}',NULL,NULL)",
                (run, parent),
            )
        conn.execute("COMMIT")


@pytest.mark.parametrize("size", [10, 10_000, 100_000])
@pytest.mark.parametrize(
    ("count", "prepare", "operation"),
    [pytest.param(count, prepare, operation, id=name) for name, count, _, prepare, operation in TRANSITIONS],
)
def test_schedules_do_not_grow_with_history_including_hidden_aborted_parents(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
    count: int,
    prepare: Prepare,
    operation: Operation,
) -> None:
    history(queue_path, size)
    hidden_parents(queue_path, size // 3)
    control = prepare(queue_path)
    before = tables(queue_path)
    tape = Tape(monkeypatch)

    result = operation(queue_path, control)
    tape.dispose()

    assert result.fault is None and result.refusal is None, result
    assert len(tape.native) == len(tape.attempts) == count
    after = tables(queue_path)
    assert after["arms"] == before["arms"]
    hidden = {row[0] for row in before["capture_aborts"]} - {"80000000-0000-4000-8000-000000000000"}
    assert len(hidden) == len(range(2, size, 3)) + size // 3
    assert {row[0] for row in after["capture_aborts"]} >= hidden
    assert [row for row in after["captures"] if row[1] in hidden] == [
        row for row in before["captures"] if row[1] in hidden
    ]


@pytest.mark.parametrize(
    ("query", "parameters"),
    [
        (cq.CAPTURES_QUERY, ("20000000-0000-4000-8000-000000000000",)),
        (cq.CAPTURE_PER_RUN_QUERY, ("20000000-0000-4000-8000-000000000000",)),
        (cq.LINK_BY_PARENT_QUERY, ("80000000-0000-4000-8000-000000000000",)),
        (cq.LINK_BY_ABORT_QUERY, ("90000000-0000-4000-8000-000000000000",)),
        (cq.RECEIPT_BY_ID_QUERY, ("80000000-0000-4000-8000-000000000000",)),
    ],
)
def test_capture_lookups_use_a_key_and_cost_the_same_at_any_history(
    queue_path: Path, query: str, parameters: tuple[object, ...]
) -> None:
    ticks = [0]

    def tick() -> int:
        ticks[0] += 1
        return 0

    steps: list[int] = []
    for total, already in ((10, 0), (10_000, 10)):
        history(queue_path, total - already, start=already)
        hidden_parents_from(queue_path, (total - already) // 3, already)
        with closing(sqlite3.connect(queue_path)) as conn:
            plan = " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {query}", parameters))
            ticks[0] = 0
            conn.set_progress_handler(tick, 1)
            conn.execute(query, parameters).fetchall()
            steps.append(ticks[0])
        assert "SEARCH" in plan and "USING" in plan, plan
    assert steps[0] == steps[1], steps


def hidden_parents_from(path: Path, size: int, start: int) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("BEGIN")
        for number in range(start, start + size):
            run, parent, abort = (
                uuid(0xB0000000 + number),
                uuid(0xC0000000 + number),
                uuid(0xD0000000 + number),
            )
            conn.execute(
                "INSERT INTO receipts(receipt_id,action,run_id,payload,acked) VALUES(?,'capture',?,'{}',0)",
                (parent, run),
            )
            conn.execute(
                "INSERT INTO receipts(receipt_id,action,run_id,payload,acked) VALUES(?,'capture_abort',?,'{}',1)",
                (abort, run),
            )
            conn.execute("INSERT INTO capture_aborts VALUES(?,?,'aborted','{}')", (parent, abort))
            conn.execute("INSERT INTO captures VALUES(?,?,'pi@dev',2000.0,'{}',NULL,NULL)", (run, parent))
        conn.execute("COMMIT")
