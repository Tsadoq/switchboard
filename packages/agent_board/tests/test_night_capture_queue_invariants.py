import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue_counts import TRANSITIONS
    from packages.agent_board.tests.test_night_effective_queue import queue_path, tables
    from packages.agent_board.tests.test_night_effective_queue_invariants import Harness
else:
    from test_night_capture_queue_counts import TRANSITIONS
    from test_night_effective_queue import queue_path, tables
    from test_night_effective_queue_invariants import Harness

__all__ = ["queue_path"]
MUTATING = {name: (prepare, operation) for name, _, _, prepare, operation in TRANSITIONS if "-" in name}
WRITES = {
    "publish-capture": 3,
    "acknowledge-capture": 4,
    "publish-abort": 3,
    "resolve-aborted": 4,
    "resolve-applied": 6,
}
CORRUPTIONS = [
    ("publish-capture", "INSERT INTO captures", "UPDATE captures SET cutoff_utc=2001.0"),
    ("publish-capture", "INSERT INTO captures", "UPDATE bindings SET uid=1001"),
    ("acknowledge-capture", "UPDATE captures", "UPDATE captures SET principal='claude@dev'"),
    ("acknowledge-capture", "UPDATE captures", "UPDATE captures SET assignment=NULL"),
    (
        "publish-abort",
        "INSERT INTO capture_aborts",
        "UPDATE capture_aborts SET outcome='aborted', proof='{}'",
    ),
    ("publish-abort", "INSERT INTO capture_aborts", "UPDATE bindings SET uid=1001"),
    ("resolve-aborted", "UPDATE capture_aborts", "UPDATE captures SET snapshot='{}'"),
    ("resolve-aborted", "UPDATE capture_aborts", "UPDATE receipts SET acked=1 WHERE action='capture'"),
    ("resolve-applied", "UPDATE capture_aborts", "UPDATE capture_aborts SET proof=proof || ' '"),
    ("resolve-applied", "UPDATE capture_aborts", "UPDATE captures SET assignment=NULL"),
]


@pytest.mark.parametrize(
    ("name", "after", "injected"),
    [pytest.param(*case, id=f"{case[0]}-{case[2][7:35]}") for case in CORRUPTIONS],
)
def test_a_valid_but_wrong_value_written_during_the_transition_is_a_snapshot_failure(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, after: str, injected: str
) -> None:
    prepare, operation = MUTATING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    harness = Harness(monkeypatch, after=after, injected=(injected, ()))

    result = operation(queue_path, control)
    harness.dispose()

    assert (result.fault, result.refusal) == (cq.Fault.SNAPSHOT_FAILURE, None), result
    assert (
        result.mutation == MutationOutcome.ROLLED_BACK
        and result.completion is None
        and result.control is None
    )
    assert "COMMIT" not in harness.attempts and tables(queue_path) == before


def _reads(name: str) -> list[tuple[str, str, int]]:
    reads = [
        (eq.CONTROL_QUERY, "control", 2),
        (eq.SELECTED_QUERY, "selected", 1 if name == "publish-capture" else 2),
        (cq.CAPTURES_QUERY, "captures", 2),
    ]
    if name in ("publish-abort", "resolve-aborted", "resolve-applied"):
        reads.append((cq.LINK_BY_ABORT_QUERY, "link", 2))
    return reads


ANOMALIES = [
    pytest.param(name, {"rowcount": (index, value)}, id=f"{name}-write{index}-rowcount{value}")
    for name, count in WRITES.items()
    for index in range(count)
    for value in (0, 2, -1)
]
ANOMALIES += [
    pytest.param(name, {"read": (query, shape, number)}, id=f"{name}-post-{label}-{shape}")
    for name in WRITES
    for query, label, number in _reads(name)
    for shape in ("empty", "duplicate")
]
ANOMALIES += [
    pytest.param(name, {"lastrowid": 99}, id=f"{name}-lastrowid")
    for name in ("publish-capture", "publish-abort")
]


@pytest.mark.parametrize(("name", "anomaly"), ANOMALIES)
@pytest.mark.parametrize("rollback", [None, "before", "after"])
@pytest.mark.parametrize("close", [None, "before", "after"])
def test_an_impossible_write_or_read_is_a_snapshot_fault_with_cleanup_first_precedence(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    anomaly: dict[str, Any],
    rollback: str | None,
    close: str | None,
) -> None:
    prepare, operation = MUTATING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    harness = Harness(monkeypatch, rollback=rollback, close=close, **anomaly)

    result = operation(queue_path, control)
    harness.dispose()

    expected = (
        cq.Fault.CLOSE_FAILURE
        if close
        else cq.Fault.ROLLBACK_FAILURE
        if rollback
        else cq.Fault.SNAPSHOT_FAILURE
    )
    assert (result.fault, result.refusal) == (expected, None), result
    assert result.control is None and result.episode is None and result.completion is None
    assert result.cleanup == (CleanupOutcome.UNCERTAIN if close else CleanupOutcome.CLOSED)
    uncertain = bool(close and rollback)
    assert result.mutation == (MutationOutcome.UNKNOWN if uncertain else MutationOutcome.ROLLED_BACK)
    assert "COMMIT" not in harness.attempts and harness.attempts.count("ROLLBACK") == 1 == harness.closes
    assert len(harness.native) + len(harness.skipped) == len(harness.attempts)
    assert tables(queue_path) == before


@pytest.mark.parametrize("code", [None, True, "5", 5, 6, 261, 262, 517])
@pytest.mark.parametrize("phase", ["connect", 0, 3, 4, "COMMIT"])
@pytest.mark.parametrize("name", ["publish-capture", "resolve-applied"])
def test_only_exact_typed_busy_or_locked_before_begin_is_a_busy_refusal(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, code: Any, phase: str | int, name: str
) -> None:
    prepare, operation = MUTATING[name]
    control = prepare(queue_path)
    before = tables(queue_path)
    harness = Harness(monkeypatch, error=(code, phase))

    result = operation(queue_path, control)
    harness.dispose()

    busy = type(code) is int and code & 0xFF in (5, 6) and phase in ("connect", 0, 3)
    assert result.refusal == (cq.Refusal.BUSY if busy else None)
    assert result.fault == (None if busy else cq.Fault.SQLITE_FAILURE)
    assert result.mutation == (
        MutationOutcome.UNKNOWN
        if phase == "COMMIT"
        else MutationOutcome.ROLLED_BACK
        if phase == 4
        else MutationOutcome.NOT_ATTEMPTED
    )
    assert result.cleanup == CleanupOutcome.CLOSED and result.completion is None
    assert harness.closes == len(harness.connections) == (0 if phase == "connect" else 1)
    for conn in harness.connections:
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(conn, "SELECT 1")
    assert tables(queue_path) == before
