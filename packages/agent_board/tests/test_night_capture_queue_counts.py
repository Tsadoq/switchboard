from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue import (
        ABORTING,
        PUBLICATION,
        aborting,
        capture_ack,
        captured,
        proof,
        resolution,
        snapshot,
    )
    from packages.agent_board.tests.test_night_effective_queue import bound_and_acked, queue_path
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_capture_queue import (
        ABORTING,
        PUBLICATION,
        aborting,
        capture_ack,
        captured,
        proof,
        resolution,
        snapshot,
    )
    from test_night_effective_queue import bound_and_acked, queue_path
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
type Prepare = Callable[[Path], eq.ExpectedControl]
type Operation = Callable[[Path, eq.ExpectedControl], cq.CaptureResult]

TRANSITIONS: list[tuple[str, int, int, Prepare, Operation]] = [
    ("observe-empty", 10, 10, bound_and_acked, lambda p, c: cq.observe_capture_episode(p, expected=c)),
    ("observe-capture", 14, 18, captured, lambda p, c: cq.observe_capture_episode(p, expected=c)),
    (
        "identical-capture",
        14,
        18,
        captured,
        lambda p, c: cq.publish_capture(p, expected=c, publication=PUBLICATION),
    ),
    ("observe-abort", 15, 20, aborting, lambda p, c: cq.observe_capture_episode(p, expected=c)),
    (
        "identical-abort",
        15,
        20,
        aborting,
        lambda p, c: cq.publish_capture_abort(p, expected=c, publication=ABORTING),
    ),
    (
        "publish-capture",
        25,
        26,
        bound_and_acked,
        lambda p, c: cq.publish_capture(p, expected=c, publication=PUBLICATION),
    ),
    (
        "acknowledge-capture",
        23,
        25,
        captured,
        lambda p, c: cq.acknowledge_capture(p, expected=c, claim=capture_ack()),
    ),
    (
        "publish-abort",
        28,
        29,
        captured,
        lambda p, c: cq.publish_capture_abort(p, expected=c, publication=ABORTING),
    ),
    (
        "resolve-aborted",
        26,
        27,
        aborting,
        lambda p, c: cq.resolve_capture_abort(p, expected=c, claim=resolution()),
    ),
    (
        "resolve-applied",
        28,
        29,
        aborting,
        lambda p, c: cq.resolve_capture_abort(p, expected=c, claim=resolution(proof("applied", snapshot()))),
    ),
]


@pytest.mark.parametrize(
    ("count", "ceiling", "prepare", "operation"),
    [
        pytest.param(count, ceiling, prepare, operation, id=name)
        for name, count, ceiling, prepare, operation in TRANSITIONS
    ],
)
def test_each_transition_has_a_fixed_schedule_within_its_contract_ceiling(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    ceiling: int,
    prepare: Prepare,
    operation: Operation,
) -> None:
    control = prepare(queue_path)
    tape = Tape(monkeypatch)

    result = operation(queue_path, control)

    assert result.fault is None and result.refusal is None and tape.closes == 1, result
    assert len(tape.native) == len(tape.attempts) == count <= ceiling
    tape.reconcile()
