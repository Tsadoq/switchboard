import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import effective_queue as queue
from agent_board.night.journal_latch import MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import (
        BINDING,
        CONNECT,
        EMPTY,
        EVIDENCE,
        RECEIPT,
        ack_for,
        bound_and_acked,
        queue_path,
        refused,
        tables,
        usable,
    )
else:
    from test_night_effective_queue import (
        BINDING,
        CONNECT,
        EMPTY,
        EVIDENCE,
        RECEIPT,
        ack_for,
        bound_and_acked,
        queue_path,
        refused,
        tables,
        usable,
    )

__all__ = ["queue_path"]


def rewrite(path: Path, old: str, new: str) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute(
            "UPDATE receipts SET payload=replace(payload,?,?) WHERE seq=(SELECT max(seq) FROM receipts)",
            (old, new),
        )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param('"counter":0', '"counter":1e999', id="counter-overflows-to-infinity"),
        pytest.param('"counter":0', '"counter":-1e999', id="counter-overflows-to-negative-infinity"),
        pytest.param('"counter":0', '"counter":' + "9" * 5000, id="counter-too-many-digits"),
        pytest.param('"counter":0', '"counter":' + "[" * 3000 + "]" * 3000, id="counter-over-deep"),
    ],
)
def test_a_pathological_stored_binding_number_is_a_row_refusal_not_a_file_fault(
    queue_path: Path, old: str, new: str
) -> None:
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    rewrite(queue_path, old, new)
    before = tables(queue_path)

    for result in (
        queue.observe_ordinary(queue_path, expected=active),
        queue.publish_ordinary(queue_path, expected=active, publication=BINDING),
        queue.acknowledge_ordinary(queue_path, expected=active, ack=ack_for(active, RECEIPT, "running")),
    ):
        refused(result, queue.Refusal.ROW_REFUSED)
        assert result.mutation == MutationOutcome.ROLLED_BACK
    assert tables(queue_path) == before


@pytest.mark.parametrize("literal", ["1e999", "-1e999", "1e309"])
def test_a_stored_evidence_runtime_that_overflows_is_a_row_refusal(queue_path: Path, literal: str) -> None:
    empty = bound_and_acked(queue_path)
    pending = usable(queue.publish_ordinary(queue_path, expected=empty, publication=EVIDENCE))
    rewrite(queue_path, '"runtime_seconds":null', f'"runtime_seconds":{literal}')

    refused(queue.observe_ordinary(queue_path, expected=pending), queue.Refusal.ROW_REFUSED)


def test_a_finite_float_still_round_trips(queue_path: Path) -> None:
    from dataclasses import replace

    empty = bound_and_acked(queue_path)
    pending = usable(
        queue.publish_ordinary(
            queue_path, expected=empty, publication=replace(EVIDENCE, runtime_seconds=1e308)
        )
    )
    assert usable(queue.observe_ordinary(queue_path, expected=pending)) == pending


@pytest.mark.parametrize(
    ("new", "transition"),
    [
        pytest.param('"counter":1e999', "publish", id="publish-overflow"),
        pytest.param('"counter":' + "9" * 5000, "publish", id="publish-digits"),
        pytest.param('"counter":1e999', "acknowledge", id="acknowledge-overflow"),
    ],
)
def test_a_pathological_number_written_during_a_transition_is_a_snapshot_failure(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, new: str, transition: str
) -> None:
    control = (
        EMPTY
        if transition == "publish"
        else usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    )
    before = tables(queue_path)
    original = queue._write
    trigger = "INSERT INTO receipts" if transition == "publish" else "UPDATE receipts"

    def anomalous(conn: sqlite3.Connection, statement: str, parameters: tuple[object, ...]) -> sqlite3.Cursor:
        cursor = original(conn, statement, parameters)
        if statement.startswith(trigger):
            conn.execute(
                "UPDATE receipts SET payload=replace(payload,'\"counter\":0',?) WHERE seq=(SELECT max(seq) FROM receipts)",
                (new,),
            )
        return cursor

    monkeypatch.setattr(queue, "_write", anomalous)
    if transition == "publish":
        result = queue.publish_ordinary(queue_path, expected=control, publication=BINDING)
    else:
        result = queue.acknowledge_ordinary(
            queue_path, expected=control, ack=ack_for(control, RECEIPT, "running")
        )

    assert (result.fault, result.refusal) == (queue.Fault.SNAPSHOT_FAILURE, None), result
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.control is None
    assert tables(queue_path) == before
