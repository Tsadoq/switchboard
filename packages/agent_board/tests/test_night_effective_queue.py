import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from agent_board.night import effective_queue as queue
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.ledger import Ledger, LedgerError
from agent_board.night.ledger_binding import BindingClaim
from agent_board.night_inspection import unit_cgroup

CONNECT = sqlite3.connect
JOURNAL = "10000000-0000-4000-8000-000000000000"
GENERATION = "40000000-0000-4000-8000-000000000000"
RUN = "20000000-0000-4000-8000-000000000000"
RECEIPT = "50000000-0000-4000-8000-000000000000"
EVIDENCE_RECEIPT = "60000000-0000-4000-8000-000000000000"
FENCE = "f" * 43
CLAIM = BindingClaim(
    RUN,
    1000,
    "30000000-0000-4000-8000-000000000000",
    "a" * 32,
    unit_cgroup(f"switchboard-night-{RUN}.service"),
)
EMPTY = queue.ExpectedControl(JOURNAL, GENERATION, 0, None, 0)
BINDING = queue.BindingPublication(RECEIPT, RUN, FENCE, CLAIM)
EVIDENCE = queue.EvidencePublication(EVIDENCE_RECEIPT, RUN, FENCE, CLAIM, "empty", "completed", None)


def create_fixture(path: Path, *, journal_id: str = JOURNAL, generation: str = GENERATION) -> None:
    Ledger(path)
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE identity SET journal_id=?", (journal_id,))
        conn.execute(queue.EFFECTIVE_QUEUE_DDL)
        conn.execute(
            "INSERT INTO effective_queue(singleton,generation,revision,active_seq) VALUES(1,?,0,NULL)",
            (generation,),
        )
        conn.execute(f"PRAGMA user_version={queue.QUEUE_VERSION}")
        conn.execute("COMMIT")


@pytest.fixture
def queue_path() -> Iterator[Path]:
    with TemporaryDirectory(prefix="eq.", dir=Path.home()) as directory:
        path = Path(directory) / "ledger.sqlite"
        create_fixture(path)
        yield path


def tables(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        return {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in names}


def ack_for(control: queue.ExpectedControl, receipt_id: str, state: str) -> queue.OrdinaryAckClaim:
    return queue.OrdinaryAckClaim(receipt_id, control.journal_id, control.acked_counter + 1, state)


def usable(result: queue.QueueResult) -> queue.ExpectedControl:
    assert result.refusal is None and result.fault is None, result
    assert result.cleanup == CleanupOutcome.CLOSED and result.control is not None
    return result.control


def refused(result: queue.QueueResult, reason: queue.Refusal) -> None:
    assert result.refusal == reason, result
    assert result.control is None and result.selected is None and result.fault is None


def bound_and_acked(path: Path) -> queue.ExpectedControl:
    active = usable(queue.publish_ordinary(path, expected=EMPTY, publication=BINDING))
    return usable(queue.acknowledge_ordinary(path, expected=active, ack=ack_for(active, RECEIPT, "running")))


def test_a_full_binding_then_evidence_lifecycle(queue_path: Path) -> None:
    observed = queue.observe_ordinary(queue_path, expected=EMPTY)
    assert usable(observed) == EMPTY and observed.selected is None
    assert observed.mutation == MutationOutcome.ROLLED_BACK and observed.changed is False

    published = queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING)
    active = usable(published)
    assert published.mutation == MutationOutcome.COMMITTED and published.changed is True
    assert active == replace(EMPTY, revision=1, active_seq=1)
    assert published.selected is not None
    assert (published.selected.seq, published.selected.receipt_id, published.selected.action) == (
        1,
        RECEIPT,
        "binding",
    )

    seen = queue.observe_ordinary(queue_path, expected=active)
    assert usable(seen) == active and seen.selected == published.selected

    acked = queue.acknowledge_ordinary(queue_path, expected=active, ack=ack_for(active, RECEIPT, "running"))
    empty = usable(acked)
    assert acked.mutation == MutationOutcome.COMMITTED and acked.selected is None
    assert empty == queue.ExpectedControl(JOURNAL, GENERATION, 2, None, 1)

    evidence = queue.publish_ordinary(queue_path, expected=empty, publication=EVIDENCE)
    pending = usable(evidence)
    assert pending == replace(empty, revision=3, active_seq=2)
    assert evidence.selected is not None and evidence.selected.action == "evidence"

    done = usable(
        queue.acknowledge_ordinary(
            queue_path, expected=pending, ack=ack_for(pending, EVIDENCE_RECEIPT, "finished")
        )
    )
    assert done == queue.ExpectedControl(JOURNAL, GENERATION, 4, None, 2)
    state = tables(queue_path)
    assert state["bindings"] == [(*astuple_claim(CLAIM), 1)]
    assert [(row[0], row[1], row[2], row[5]) for row in state["receipts"]] == [
        (1, RECEIPT, "binding", 1),
        (2, EVIDENCE_RECEIPT, "evidence", 1),
    ]
    assert state["identity"] == [(1, JOURNAL, 2)]


def astuple_claim(claim: BindingClaim) -> tuple[object, ...]:
    return (claim.run_id, claim.uid, claim.boot_id, claim.invocation_id, claim.cgroup)


def test_the_stored_payload_is_the_canonical_wire_payload(queue_path: Path) -> None:
    published = queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING)
    assert published.selected is not None
    assert published.selected.payload == (
        '{"boot_id":"30000000-0000-4000-8000-000000000000","cgroup":"'
        + CLAIM.cgroup
        + '","counter":0,"fence":"'
        + FENCE
        + '","invocation_id":"'
        + "a" * 32
        + '","journal_id":"'
        + JOURNAL
        + '","receipt_id":"'
        + RECEIPT
        + '"}'
    )


def test_an_identical_active_publication_rolls_back_unchanged(queue_path: Path) -> None:
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    before = tables(queue_path)

    again = queue.publish_ordinary(queue_path, expected=active, publication=BINDING)

    assert (
        usable(again) == active and again.mutation == MutationOutcome.ROLLED_BACK and again.changed is False
    )
    assert tables(queue_path) == before


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"fence": "g" * 43}, id="fence"),
        pytest.param({"receipt_id": EVIDENCE_RECEIPT}, id="receipt"),
        pytest.param({"claim": replace(CLAIM, uid=1001)}, id="uid-only-in-claim"),
    ],
)
def test_a_different_active_publication_conflicts(queue_path: Path, change: dict[str, Any]) -> None:
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    before = tables(queue_path)

    result = queue.publish_ordinary(queue_path, expected=active, publication=replace(BINDING, **change))

    refused(result, queue.Refusal.PUBLICATION_CONFLICT)
    assert result.mutation == MutationOutcome.ROLLED_BACK and tables(queue_path) == before


@pytest.mark.parametrize(
    "stale",
    [
        pytest.param(replace(EMPTY, revision=1), id="revision"),
        pytest.param(replace(EMPTY, generation="40000000-0000-4000-8000-000000000001"), id="generation"),
        pytest.param(replace(EMPTY, acked_counter=1), id="counter"),
        pytest.param(replace(EMPTY, active_seq=1), id="active"),
    ],
)
def test_a_stale_or_wrong_control_refuses_every_operation(
    queue_path: Path, stale: queue.ExpectedControl
) -> None:
    before = tables(queue_path)
    for result in (
        queue.observe_ordinary(queue_path, expected=stale),
        queue.publish_ordinary(queue_path, expected=stale, publication=BINDING),
        queue.acknowledge_ordinary(queue_path, expected=stale, ack=ack_for(stale, RECEIPT, "running")),
    ):
        refused(result, queue.Refusal.CONTROL_MISMATCH)
    assert tables(queue_path) == before


def test_a_control_for_another_journal_is_an_identity_mismatch(queue_path: Path) -> None:
    other = replace(EMPTY, journal_id="10000000-0000-4000-8000-000000000001")
    refused(queue.observe_ordinary(queue_path, expected=other), queue.Refusal.LEDGER_IDENTITY_MISMATCH)


def test_a_rollback_to_an_older_revision_refuses_against_the_retained_newer_control(queue_path: Path) -> None:
    older = queue_path.read_bytes()
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    queue_path.write_bytes(older)

    refused(queue.observe_ordinary(queue_path, expected=active), queue.Refusal.CONTROL_MISMATCH)
    refused(
        queue.acknowledge_ordinary(queue_path, expected=active, ack=ack_for(active, RECEIPT, "running")),
        queue.Refusal.CONTROL_MISMATCH,
    )


def test_publication_needs_an_empty_queue_and_acknowledgement_needs_a_selection(queue_path: Path) -> None:
    refused(
        queue.acknowledge_ordinary(queue_path, expected=EMPTY, ack=ack_for(EMPTY, RECEIPT, "running")),
        queue.Refusal.NOTHING_SELECTED,
    )


def test_a_binding_already_acknowledged_is_never_published_again(queue_path: Path) -> None:
    empty = bound_and_acked(queue_path)
    fresh = replace(BINDING, receipt_id="70000000-0000-4000-8000-000000000000")
    refused(
        queue.publish_ordinary(queue_path, expected=empty, publication=fresh),
        queue.Refusal.BINDING_ALREADY_ACKED,
    )


def test_a_different_binding_for_the_run_conflicts(queue_path: Path) -> None:
    empty = bound_and_acked(queue_path)
    other = replace(EVIDENCE, claim=replace(CLAIM, invocation_id="b" * 32))
    refused(
        queue.publish_ordinary(queue_path, expected=empty, publication=other), queue.Refusal.BINDING_CONFLICT
    )


def test_evidence_needs_an_acknowledged_binding(queue_path: Path) -> None:
    refused(
        queue.publish_ordinary(queue_path, expected=EMPTY, publication=EVIDENCE),
        queue.Refusal.BINDING_NOT_ACKED,
    )
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    refused(
        queue.publish_ordinary(queue_path, expected=active, publication=EVIDENCE),
        queue.Refusal.PUBLICATION_CONFLICT,
    )


def test_a_second_evidence_for_the_run_conflicts(queue_path: Path) -> None:
    empty = bound_and_acked(queue_path)
    pending = usable(queue.publish_ordinary(queue_path, expected=empty, publication=EVIDENCE))
    done = usable(
        queue.acknowledge_ordinary(
            queue_path, expected=pending, ack=ack_for(pending, EVIDENCE_RECEIPT, "finished")
        )
    )
    again = replace(EVIDENCE, receipt_id="70000000-0000-4000-8000-000000000000")
    refused(
        queue.publish_ordinary(queue_path, expected=done, publication=again), queue.Refusal.EVIDENCE_CONFLICT
    )


def test_a_reused_receipt_id_conflicts(queue_path: Path) -> None:
    empty = bound_and_acked(queue_path)
    reused = replace(EVIDENCE, receipt_id=RECEIPT)
    refused(
        queue.publish_ordinary(queue_path, expected=empty, publication=reused), queue.Refusal.RECEIPT_CONFLICT
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"receipt_id": EVIDENCE_RECEIPT}, id="other-receipt"),
        pytest.param({"journal_id": "10000000-0000-4000-8000-000000000001"}, id="other-journal"),
        pytest.param({"counter": 2}, id="skipped-counter"),
        pytest.param({"state": "finished"}, id="binding-not-running"),
    ],
)
def test_an_acknowledgement_that_does_not_match_refuses(queue_path: Path, change: dict[str, Any]) -> None:
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    before = tables(queue_path)
    claim = replace(ack_for(active, RECEIPT, "running"), **change)

    refused(queue.acknowledge_ordinary(queue_path, expected=active, ack=claim), queue.Refusal.ACK_REFUSED)
    assert tables(queue_path) == before


@pytest.mark.parametrize(
    ("outcome", "state", "accepted"),
    [
        ("empty", "finished", True),
        ("empty", "quarantined", False),
        ("empty", "running", False),
        ("uncertain", "finished", True),
        ("uncertain", "quarantined", True),
        ("uncertain", "running", False),
    ],
)
def test_evidence_acknowledgement_states(queue_path: Path, outcome: str, state: str, accepted: bool) -> None:
    empty = bound_and_acked(queue_path)
    pending = usable(
        queue.publish_ordinary(
            queue_path, expected=empty, publication=replace(EVIDENCE, outcome=outcome, reason="deadline")
        )
    )
    result = queue.acknowledge_ordinary(
        queue_path, expected=pending, ack=ack_for(pending, EVIDENCE_RECEIPT, state)
    )
    if accepted:
        assert usable(result).active_seq is None
    else:
        refused(result, queue.Refusal.ACK_REFUSED)


def test_a_stale_acknowledgement_cannot_touch_a_later_episode(queue_path: Path) -> None:
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    claim = ack_for(active, RECEIPT, "running")
    usable(queue.acknowledge_ordinary(queue_path, expected=active, ack=claim))
    before = tables(queue_path)

    refused(
        queue.acknowledge_ordinary(queue_path, expected=active, ack=claim), queue.Refusal.CONTROL_MISMATCH
    )
    assert tables(queue_path) == before


def test_the_unchanged_legacy_constructor_refuses_version_4_and_leaves_the_bytes(queue_path: Path) -> None:
    before = queue_path.read_bytes()
    with pytest.raises(LedgerError, match="unsupported_ledger_version"):
        Ledger(queue_path)
    assert queue_path.read_bytes() == before


def test_a_version_3_ledger_is_refused_as_schema(queue_path: Path) -> None:
    with TemporaryDirectory(prefix="eq3.", dir=Path.home()) as directory:
        legacy = Path(directory) / "ledger.sqlite"
        Ledger(legacy)
        with closing(CONNECT(legacy, isolation_level=None)) as conn:
            conn.execute("UPDATE identity SET journal_id=?", (JOURNAL,))
        refused(queue.observe_ordinary(legacy, expected=EMPTY), queue.Refusal.SCHEMA_REFUSED)
