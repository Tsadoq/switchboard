import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq
from agent_board.night.assignment import NightPolicy, build_assignment, encode, parse_wakes
from agent_board.night.journal_latch import MutationOutcome
from agent_board.night.ledger import CaptureContext, Ledger, Pending

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import (
        BINDING,
        CLAIM,
        CONNECT,
        EMPTY,
        FENCE,
        JOURNAL,
        RUN,
        bound_and_acked,
        create_fixture,
        queue_path,
        tables,
    )
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_effective_queue import (
        BINDING,
        CLAIM,
        CONNECT,
        EMPTY,
        FENCE,
        JOURNAL,
        RUN,
        bound_and_acked,
        create_fixture,
        queue_path,
        tables,
    )
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
CAPTURE = "80000000-0000-4000-8000-000000000000"
ABORT = "90000000-0000-4000-8000-000000000000"
POLICY = NightPolicy("sb-runner", "a" * 40, "claude", None, "default", 600)
CHOICES = cq.CaptureChoices("pi@dev", 2000.0, POLICY)
PUBLICATION = cq.CapturePublication(CAPTURE, RUN, FENCE, CLAIM, CHOICES)
ABORTING = cq.CaptureAbortPublication(ABORT, RUN, CAPTURE)
WAKE = {
    "id": 7,
    "reason": "mention",
    "source_ref": "post:12",
    "summary": "look at the queue",
    "summary_truncated": False,
    "thread_id": 31,
    "topic_id": None,
}


def snapshot(counter: int = 2, wakes: list[dict[str, Any]] | None = None, **changes: Any) -> dict[str, Any]:
    value = {
        "journal_id": JOURNAL,
        "counter": counter,
        "capture_id": CAPTURE,
        "run_id": RUN,
        "principal": "pi@dev",
        "deadline": 1900,
        "wakes": [WAKE] if wakes is None else wakes,
    }
    return value | changes


def packet(value: object) -> bytes:
    return cq._canonical(value).encode()


def capture_ack(value: dict[str, Any] | None = None, receipt_id: str = CAPTURE) -> cq.CaptureAckClaim:
    return cq.CaptureAckClaim(receipt_id, packet(snapshot() if value is None else value))


def proof(outcome: str = "aborted", capture: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    value = {
        "journal_id": JOURNAL,
        "run_id": RUN,
        "abort_id": ABORT,
        "capture_id": CAPTURE,
        "capture_counter": 1,
        "counter": 2,
        "outcome": outcome,
        "capture": capture,
    }
    return value | changes


def resolution(value: dict[str, Any] | None = None, receipt_id: str = ABORT) -> cq.AbortResolutionClaim:
    return cq.AbortResolutionClaim(receipt_id, packet(proof() if value is None else value))


def ok(result: cq.CaptureResult) -> eq.ExpectedControl:
    assert result.refusal is None and result.fault is None and result.control is not None, result
    return result.control


def refused(result: cq.CaptureResult, reason: cq.Refusal) -> None:
    assert result.refusal == reason, result
    assert result.control is None and result.episode is None and result.completion is None


def captured(path: Path) -> eq.ExpectedControl:
    return ok(cq.publish_capture(path, expected=bound_and_acked(path), publication=PUBLICATION))


def aborting(path: Path) -> eq.ExpectedControl:
    return ok(cq.publish_capture_abort(path, expected=captured(path), publication=ABORTING))


def derived(value: dict[str, Any]) -> bytes | None:
    assignment = build_assignment(
        run_id=RUN,
        principal="pi@dev",
        wakes=parse_wakes(value["wakes"]),
        policy=POLICY,
        deadline_utc=min(float(value["deadline"]), 2000.0),
    )
    return None if assignment is None else encode(assignment)


def row(path: Path, sql: str, *parameters: object) -> list[tuple[Any, ...]]:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        return conn.execute(sql, parameters).fetchall()


def test_publish_observe_and_acknowledge_a_capture(queue_path: Path) -> None:
    empty = bound_and_acked(queue_path)
    published = cq.publish_capture(queue_path, expected=empty, publication=PUBLICATION)
    active = ok(published)
    assert published.mutation == MutationOutcome.COMMITTED and active == replace(
        empty, revision=3, active_seq=2
    )
    assert published.episode is not None and published.episode.action == "capture"
    assert published.episode.choices == CHOICES and published.completion is None

    seen = cq.observe_capture_episode(queue_path, expected=active)
    assert ok(seen) == active and seen.episode == published.episode

    acked = cq.acknowledge_capture(queue_path, expected=active, claim=capture_ack())
    assert ok(acked) == eq.ExpectedControl(JOURNAL, empty.generation, 4, None, 2)
    assert acked.completion == cq.CapturedCompletion(packet(snapshot()).decode(), derived(snapshot()))
    assert row(queue_path, "SELECT snapshot, assignment FROM captures") == [
        (packet(snapshot()).decode(), derived(snapshot()))
    ]
    assert row(queue_path, "SELECT acked FROM receipts WHERE receipt_id=?", CAPTURE) == [(1,)]


def test_an_empty_capture_stores_and_returns_no_assignment(queue_path: Path) -> None:
    active = captured(queue_path)
    acked = cq.acknowledge_capture(queue_path, expected=active, claim=capture_ack(snapshot(wakes=[])))
    assert ok(acked).acked_counter == 2 and acked.completion == cq.CapturedCompletion(
        packet(snapshot(wakes=[])).decode(), None
    )


def test_an_identical_active_capture_rolls_back_and_a_different_one_conflicts(queue_path: Path) -> None:
    active = captured(queue_path)
    before = tables(queue_path)
    same = cq.publish_capture(queue_path, expected=active, publication=PUBLICATION)
    assert (
        ok(same) == active and same.mutation == MutationOutcome.ROLLED_BACK and tables(queue_path) == before
    )
    for different in (
        replace(PUBLICATION, choices=replace(CHOICES, cutoff_utc=2001.0)),
        replace(PUBLICATION, choices=replace(CHOICES, policy=replace(POLICY, turn_seconds=601))),
        replace(PUBLICATION, fence="g" * 43),
    ):
        refused(
            cq.publish_capture(queue_path, expected=active, publication=different),
            cq.Refusal.PUBLICATION_CONFLICT,
        )
    assert tables(queue_path) == before


def test_resolve_aborted_retains_the_parent_and_capture_is_once_per_run(queue_path: Path) -> None:
    active = aborting(queue_path)
    seen = cq.observe_capture_episode(queue_path, expected=active)
    assert ok(seen) == active and seen.episode is not None
    assert (seen.episode.action, seen.episode.abort_receipt_id) == ("capture_abort", ABORT)
    abort_payload = json.loads(seen.episode.abort_payload or "")
    parent_payload = json.loads(seen.episode.capture_payload)
    assert abort_payload == parent_payload | {
        "receipt_id": ABORT,
        "capture_receipt_id": CAPTURE,
        "capture_counter": 1,
    }

    again = cq.publish_capture_abort(queue_path, expected=active, publication=ABORTING)
    assert ok(again) == active and again.mutation == MutationOutcome.ROLLED_BACK

    resolved = cq.resolve_capture_abort(queue_path, expected=active, claim=resolution())
    empty = ok(resolved)
    assert empty.active_seq is None and empty.acked_counter == 2
    assert resolved.completion == cq.AbortedCompletion(packet(proof()).decode())
    assert row(
        queue_path, "SELECT receipt_id, acked FROM receipts WHERE action LIKE 'capture%' ORDER BY seq"
    ) == [
        (CAPTURE, 0),
        (ABORT, 1),
    ]
    assert row(queue_path, "SELECT snapshot, assignment FROM captures") == [(None, None)]
    assert row(queue_path, "SELECT outcome, proof FROM capture_aborts") == [
        ("aborted", packet(proof()).decode())
    ]

    fresh = replace(PUBLICATION, receipt_id="a0000000-0000-4000-8000-000000000000")
    before = tables(queue_path)
    refused(
        cq.publish_capture(queue_path, expected=empty, publication=fresh), cq.Refusal.CAPTURE_ALREADY_EXISTS
    )
    assert tables(queue_path) == before


def test_resolve_applied_returns_only_history_and_stores_the_derived_assignment(queue_path: Path) -> None:
    active = aborting(queue_path)
    applied = proof("applied", snapshot())

    resolved = cq.resolve_capture_abort(queue_path, expected=active, claim=resolution(applied))

    assert ok(resolved).acked_counter == 2
    completion = resolved.completion
    assert type(completion) is cq.AppliedAfterAbortCompletion
    assert completion == cq.AppliedAfterAbortCompletion(packet(snapshot()).decode(), packet(applied).decode())
    assert not hasattr(completion, "assignment")
    assert row(queue_path, "SELECT snapshot, assignment FROM captures") == [
        (packet(snapshot()).decode(), derived(snapshot()))
    ]
    assert row(queue_path, "SELECT acked FROM receipts WHERE action LIKE 'capture%' ORDER BY seq") == [
        (1,),
        (1,),
    ]
    assert row(queue_path, "SELECT outcome FROM capture_aborts") == [("applied",)]


def test_a_late_parent_ack_and_the_wrong_action_are_told_apart(queue_path: Path) -> None:
    parent = captured(queue_path)
    current = ok(cq.publish_capture_abort(queue_path, expected=parent, publication=ABORTING))
    before = tables(queue_path)

    refused(
        cq.acknowledge_capture(queue_path, expected=parent, claim=capture_ack()), cq.Refusal.CONTROL_MISMATCH
    )
    refused(
        cq.acknowledge_capture(queue_path, expected=current, claim=capture_ack()), cq.Refusal.WRONG_ACTION
    )
    assert tables(queue_path) == before


def test_resolving_a_plain_capture_is_the_wrong_action(queue_path: Path) -> None:
    active = captured(queue_path)
    refused(
        cq.resolve_capture_abort(queue_path, expected=active, claim=resolution()), cq.Refusal.WRONG_ACTION
    )


def test_the_capture_api_on_an_ordinary_selection_is_the_wrong_action(queue_path: Path) -> None:
    active = ok_ordinary(eq.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    refused(cq.observe_capture_episode(queue_path, expected=active), cq.Refusal.WRONG_ACTION)


def ok_ordinary(result: eq.QueueResult) -> eq.ExpectedControl:
    assert result.control is not None, result
    return result.control


def test_the_ordinary_api_still_refuses_a_selected_capture_or_abort(queue_path: Path) -> None:
    for control in (captured(queue_path),):
        assert eq.observe_ordinary(queue_path, expected=control).refusal == eq.Refusal.UNSUPPORTED_ACTION
    with TemporaryDirectory(prefix="cq.", dir=Path.home()) as directory:
        other = Path(directory) / "ledger.sqlite"
        create_fixture(other)
        control = aborting(other)
        assert eq.observe_ordinary(other, expected=control).refusal == eq.Refusal.UNSUPPORTED_ACTION


@pytest.mark.parametrize(
    ("claim", "reason"),
    [
        pytest.param(capture_ack(snapshot(counter=3)), cq.Refusal.ACK_REFUSED, id="counter-q-plus-2"),
        pytest.param(capture_ack(snapshot(capture_id=ABORT)), cq.Refusal.ACK_REFUSED, id="capture-id"),
        pytest.param(capture_ack(snapshot(principal="claude@dev")), cq.Refusal.ACK_REFUSED, id="principal"),
        pytest.param(capture_ack(receipt_id=ABORT), cq.Refusal.ACK_REFUSED, id="request-correlation"),
        pytest.param(
            capture_ack(snapshot(journal_id="10000000-0000-4000-8000-000000000001")),
            cq.Refusal.ACK_REFUSED,
            id="journal",
        ),
    ],
)
def test_a_mismatched_capture_claim_is_refused_before_any_write(
    queue_path: Path, claim: cq.CaptureAckClaim, reason: cq.Refusal
) -> None:
    active = captured(queue_path)
    before = tables(queue_path)
    result = cq.acknowledge_capture(queue_path, expected=active, claim=claim)
    refused(result, reason)
    assert result.mutation == MutationOutcome.ROLLED_BACK and tables(queue_path) == before


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(proof(counter=3), id="counter-q-plus-2"),
        pytest.param(proof(capture_counter=0), id="capture-counter"),
        pytest.param(proof(abort_id=CAPTURE, capture_id=ABORT), id="swapped-ids"),
        pytest.param(proof("applied", snapshot(counter=3)), id="applied-snapshot-counter"),
        pytest.param(proof("applied", snapshot(principal="claude@dev")), id="applied-principal"),
    ],
)
def test_a_mismatched_abort_proof_is_refused_before_any_write(
    queue_path: Path, value: dict[str, Any]
) -> None:
    active = aborting(queue_path)
    before = tables(queue_path)
    refused(
        cq.resolve_capture_abort(queue_path, expected=active, claim=resolution(value)), cq.Refusal.ACK_REFUSED
    )
    assert tables(queue_path) == before


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps(snapshot()).encode(), id="not-canonical"),
        pytest.param(packet(snapshot() | {"extra": 1}), id="extra-key"),
        pytest.param(b'{"counter":NaN}', id="nan"),
        pytest.param(packet(snapshot(deadline=True)), id="bool-deadline"),
        pytest.param(packet(snapshot(counter=True)), id="bool-counter"),
        pytest.param(packet(snapshot(wakes=[WAKE, WAKE])), id="repeated-wake"),
        pytest.param(packet(snapshot(wakes=[WAKE | {"reason": "x" * 32769}])), id="long-reason"),
        pytest.param(packet(snapshot(wakes=[WAKE | {"id": i} for i in range(1, 22)])), id="too-many-wakes"),
        pytest.param(b"x" * 65537, id="oversized"),
        pytest.param("é".encode(), id="not-ascii"),
    ],
)
def test_an_unusable_capture_packet_is_refused_before_opening_sqlite(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    active = captured(queue_path)
    tape = Tape(monkeypatch)
    result = cq.acknowledge_capture(queue_path, expected=active, claim=cq.CaptureAckClaim(CAPTURE, raw))
    refused(result, cq.Refusal.CAPTURE_UNUSABLE)
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and tape.attempts == [] and tape.connections == []


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(proof("applied", None), id="applied-without-capture"),
        pytest.param(proof("aborted", snapshot()), id="aborted-with-capture"),
        pytest.param(proof("ignored"), id="outcome"),
        pytest.param(proof() | {"extra": 1}, id="extra-key"),
    ],
)
def test_an_unusable_proof_packet_is_refused_before_opening_sqlite(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, value: dict[str, Any]
) -> None:
    active = aborting(queue_path)
    tape = Tape(monkeypatch)
    result = cq.resolve_capture_abort(queue_path, expected=active, claim=resolution(value))
    refused(result, cq.Refusal.CAPTURE_UNUSABLE)
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and tape.attempts == []


def test_a_snapshot_that_cannot_become_an_assignment_rolls_back_as_unusable(queue_path: Path) -> None:
    active = captured(queue_path)
    before = tables(queue_path)
    long = [WAKE | {"id": 1, "reason": "x" * 30000}, WAKE | {"id": 2, "reason": "y" * 30000}]
    result = cq.acknowledge_capture(queue_path, expected=active, claim=capture_ack(snapshot(wakes=long)))
    refused(result, cq.Refusal.CAPTURE_UNUSABLE)
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.fault is None
    assert tables(queue_path) == before


def legacy_fixture(path: Path) -> eq.ExpectedControl:
    ledger = Ledger(path)
    payload = {
        "fence": FENCE,
        "journal_id": JOURNAL,
        "counter": 1,
        "receipt_id": CAPTURE,
        "boot_id": CLAIM.boot_id,
        "invocation_id": CLAIM.invocation_id,
        "cgroup": CLAIM.cgroup,
    }
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("UPDATE identity SET journal_id=?, acked_counter=1", (JOURNAL,))
        conn.execute(
            "INSERT INTO bindings VALUES(?,?,?,?,?,1)",
            (RUN, 1000, CLAIM.boot_id, CLAIM.invocation_id, CLAIM.cgroup),
        )
    ledger.persist_capture(
        Pending(CAPTURE, "capture", RUN, payload), CaptureContext("pi@dev", 2000.0, POLICY)
    )
    generation = EMPTY.generation
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(eq.EFFECTIVE_QUEUE_DDL)
        seq = conn.execute("SELECT seq FROM receipts WHERE receipt_id=?", (CAPTURE,)).fetchone()[0]
        conn.execute("INSERT INTO effective_queue VALUES(1,?,5,?)", (generation, seq))
        conn.execute(f"PRAGMA user_version={eq.QUEUE_VERSION}")
        conn.execute("COMMIT")
    return eq.ExpectedControl(JOURNAL, generation, 5, seq, 1)


@pytest.mark.parametrize(
    ("compact_receipt", "compact_policy"), [(False, False), (True, False), (False, True)]
)
def test_legacy_written_capture_state_is_refused_without_normalization(
    compact_receipt: bool, compact_policy: bool
) -> None:
    with TemporaryDirectory(prefix="cql.", dir=Path.home()) as directory:
        path = Path(directory) / "ledger.sqlite"
        control = legacy_fixture(path)
        stored = row(path, "SELECT payload FROM receipts WHERE receipt_id=?", CAPTURE)[0][0]
        policy = row(path, "SELECT policy FROM captures")[0][0]
        assert ", " in stored and ", " in policy, "the legacy separators this test relies on changed"
        if compact_receipt:
            with closing(CONNECT(path, isolation_level=None)) as conn:
                conn.execute(
                    "UPDATE receipts SET payload=? WHERE receipt_id=?",
                    (cq._canonical(json.loads(stored)), CAPTURE),
                )
        if compact_policy:
            with closing(CONNECT(path, isolation_level=None)) as conn:
                conn.execute("UPDATE captures SET policy=?", (cq._canonical(json.loads(policy)),))
        before = path.read_bytes()
        for result in (
            cq.observe_capture_episode(path, expected=control),
            cq.acknowledge_capture(path, expected=control, claim=capture_ack()),
            cq.publish_capture_abort(path, expected=control, publication=ABORTING),
        ):
            refused(result, cq.Refusal.ROW_REFUSED)
        assert path.read_bytes() == before


def test_abort_needs_a_selected_capture(queue_path: Path) -> None:
    empty = bound_and_acked(queue_path)
    refused(
        cq.publish_capture_abort(queue_path, expected=empty, publication=ABORTING),
        cq.Refusal.NOTHING_SELECTED,
    )
    refused(
        cq.acknowledge_capture(queue_path, expected=empty, claim=capture_ack()), cq.Refusal.NOTHING_SELECTED
    )


def test_capture_needs_an_acknowledged_matching_binding(queue_path: Path) -> None:
    refused(
        cq.publish_capture(queue_path, expected=EMPTY, publication=PUBLICATION), cq.Refusal.BINDING_NOT_ACKED
    )
    empty = bound_and_acked(queue_path)
    other = replace(PUBLICATION, claim=replace(CLAIM, uid=1001))
    refused(cq.publish_capture(queue_path, expected=empty, publication=other), cq.Refusal.BINDING_CONFLICT)
