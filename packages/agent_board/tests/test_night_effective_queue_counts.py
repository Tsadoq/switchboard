from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import effective_queue as queue

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import (
        BINDING,
        EMPTY,
        EVIDENCE,
        EVIDENCE_RECEIPT,
        RECEIPT,
        ack_for,
        bound_and_acked,
        queue_path,
        usable,
    )
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_effective_queue import (
        BINDING,
        EMPTY,
        EVIDENCE,
        EVIDENCE_RECEIPT,
        RECEIPT,
        ack_for,
        bound_and_acked,
        queue_path,
        usable,
    )
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
CEILING = 32
type Prepare = Callable[[Path], queue.ExpectedControl]
type Operation = Callable[[Path, queue.ExpectedControl], queue.QueueResult]


def nothing(path: Path) -> queue.ExpectedControl:
    return EMPTY


def active(path: Path) -> queue.ExpectedControl:
    return usable(queue.publish_ordinary(path, expected=EMPTY, publication=BINDING))


def evidence_active(path: Path) -> queue.ExpectedControl:
    return usable(queue.publish_ordinary(path, expected=bound_and_acked(path), publication=EVIDENCE))


TRANSITIONS: list[tuple[str, int, Prepare, Operation]] = [
    ("observe-empty", 10, nothing, lambda p, c: queue.observe_ordinary(p, expected=c)),
    ("observe-active", 13, active, lambda p, c: queue.observe_ordinary(p, expected=c)),
    ("publish-binding", 22, nothing, lambda p, c: queue.publish_ordinary(p, expected=c, publication=BINDING)),
    (
        "publish-identical",
        13,
        active,
        lambda p, c: queue.publish_ordinary(p, expected=c, publication=BINDING),
    ),
    (
        "acknowledge-binding",
        21,
        active,
        lambda p, c: queue.acknowledge_ordinary(p, expected=c, ack=ack_for(c, RECEIPT, "running")),
    ),
    (
        "publish-evidence",
        22,
        bound_and_acked,
        lambda p, c: queue.publish_ordinary(p, expected=c, publication=EVIDENCE),
    ),
    (
        "acknowledge-evidence",
        20,
        evidence_active,
        lambda p, c: queue.acknowledge_ordinary(p, expected=c, ack=ack_for(c, EVIDENCE_RECEIPT, "finished")),
    ),
    ("stale-control", 10, nothing, lambda p, c: queue.observe_ordinary(p, expected=replace(c, revision=5))),
]


@pytest.mark.parametrize(
    ("count", "prepare", "operation"),
    [pytest.param(count, prepare, operation, id=name) for name, count, prepare, operation in TRANSITIONS],
)
def test_each_transition_has_a_fixed_native_statement_count_within_the_ceiling(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    prepare: Prepare,
    operation: Operation,
) -> None:
    control = prepare(queue_path)
    tape = Tape(monkeypatch)

    result = operation(queue_path, control)

    assert result.fault is None and tape.closes == 1
    assert len(tape.native) == len(tape.attempts) == count <= CEILING
    tape.reconcile()
