import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import effective_queue as queue
from agent_board.night.ledger_binding import BindingClaim

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import (
        BINDING,
        CLAIM,
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
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_effective_queue import (
        BINDING,
        CLAIM,
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
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
MAX = 2**63 - 1


def tamper(value: object, **changes: object) -> object:
    for name, changed in changes.items():
        object.__setattr__(value, name, changed)
    return value


def sql(path: Path, *statements: str, parameters: tuple[object, ...] = ()) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        for statement in statements:
            conn.execute(statement, parameters)


class SubControl(queue.ExpectedControl):
    pass


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param({"journal_id": EMPTY.journal_id}, id="mapping"),
        pytest.param(SubControl(EMPTY.journal_id, EMPTY.generation, 0, None, 0), id="subclass"),
        pytest.param(tamper(replace(EMPTY), revision=True), id="bool-revision"),
        pytest.param(tamper(replace(EMPTY), revision=-1), id="negative-revision"),
        pytest.param(tamper(replace(EMPTY), revision=MAX + 1), id="overflow-revision"),
        pytest.param(tamper(replace(EMPTY), active_seq=0), id="zero-active"),
        pytest.param(tamper(replace(EMPTY), acked_counter=1.0), id="float-counter"),
        pytest.param(
            tamper(replace(EMPTY), generation="4000000A-0000-4000-8000-000000000000"),
            id="uppercase-generation",
        ),
        pytest.param(
            tamper(replace(EMPTY), generation="40000000-0000-1000-8000-000000000000"), id="non-v4-generation"
        ),
        pytest.param(tamper(replace(EMPTY), journal_id="not-a-uuid"), id="journal"),
    ],
)
def test_a_malformed_expected_control_is_invalid_input_before_any_sql(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, expected: object
) -> None:
    tape = Tape(monkeypatch)
    result = queue.observe_ordinary(queue_path, expected=expected)  # type: ignore[arg-type]
    refused(result, queue.Refusal.INVALID_INPUT)
    assert tape.attempts == [] and tape.connections == []


@pytest.mark.parametrize(
    "publication",
    [
        pytest.param(
            replace(BINDING, run_id="20000000-0000-4000-8000-000000000001"), id="run-differs-from-claim"
        ),
        pytest.param(replace(BINDING, fence="f" * 31), id="short-fence"),
        pytest.param(replace(BINDING, fence="f" * 129), id="long-fence"),
        pytest.param(replace(BINDING, fence="f" * 42 + "!"), id="fence-character"),
        pytest.param(replace(BINDING, receipt_id="5000000A-0000-4000-8000-000000000000"), id="receipt-case"),
        pytest.param(replace(BINDING, claim=replace(CLAIM, uid=0)), id="claim-uid"),
        pytest.param(replace(BINDING, claim=replace(CLAIM, cgroup="/other")), id="claim-cgroup"),
        pytest.param(replace(EVIDENCE, outcome="done"), id="outcome"),
        pytest.param(replace(EVIDENCE, reason="other"), id="reason"),
        pytest.param(replace(EVIDENCE, runtime_seconds=True), id="bool-runtime"),
        pytest.param(replace(EVIDENCE, runtime_seconds=-1), id="negative-runtime"),
        pytest.param(replace(EVIDENCE, runtime_seconds=float("nan")), id="nan-runtime"),
        pytest.param(replace(EVIDENCE, runtime_seconds=float("inf")), id="infinite-runtime"),
        pytest.param(replace(EVIDENCE, runtime_seconds=MAX + 1), id="overflow-runtime"),
        pytest.param(tamper(replace(EVIDENCE), runtime_seconds="1"), id="text-runtime"),
        pytest.param(tamper(replace(BINDING), claim=object()), id="claim-object"),
        pytest.param(object(), id="not-a-publication"),
    ],
)
def test_a_malformed_publication_is_invalid_input_before_any_sql(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, publication: object
) -> None:
    tape = Tape(monkeypatch)
    result = queue.publish_ordinary(queue_path, expected=EMPTY, publication=publication)  # type: ignore[arg-type]
    refused(result, queue.Refusal.INVALID_INPUT)
    assert tape.attempts == []


def test_a_claim_subclass_is_refused(queue_path: Path) -> None:
    class SubClaim(BindingClaim):
        pass

    sub = SubClaim(CLAIM.run_id, CLAIM.uid, CLAIM.boot_id, CLAIM.invocation_id, CLAIM.cgroup)
    refused(
        queue.publish_ordinary(queue_path, expected=EMPTY, publication=replace(BINDING, claim=sub)),
        queue.Refusal.INVALID_INPUT,
    )


@pytest.mark.parametrize(
    "ack",
    [
        pytest.param(tamper(ack_for(EMPTY, RECEIPT, "running"), counter=0), id="zero-counter"),
        pytest.param(tamper(ack_for(EMPTY, RECEIPT, "running"), counter=True), id="bool-counter"),
        pytest.param(tamper(ack_for(EMPTY, RECEIPT, "running"), state="done"), id="state"),
        pytest.param(tamper(ack_for(EMPTY, RECEIPT, "running"), receipt_id="x"), id="receipt"),
        pytest.param({"receipt_id": RECEIPT}, id="mapping"),
    ],
)
def test_a_malformed_acknowledgement_is_invalid_input(queue_path: Path, ack: object) -> None:
    refused(
        queue.acknowledge_ordinary(queue_path, expected=EMPTY, ack=ack),  # type: ignore[arg-type]
        queue.Refusal.INVALID_INPUT,
    )


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("ledger.sqlite", id="string"),
        pytest.param(PurePosixPath("/tmp/ledger.sqlite"), id="pure-path"),
    ],
)
def test_a_path_that_is_not_an_exact_posix_path_is_invalid_input(path: object) -> None:
    refused(queue.observe_ordinary(path, expected=EMPTY), queue.Refusal.INVALID_INPUT)  # type: ignore[arg-type]


def test_a_float_runtime_keeps_its_type_in_the_stored_payload(queue_path: Path) -> None:
    empty = bound_and_acked(queue_path)
    result = queue.publish_ordinary(
        queue_path, expected=empty, publication=replace(EVIDENCE, runtime_seconds=1.0)
    )
    assert result.selected is not None and '"runtime_seconds":1.0' in result.selected.payload


def active_with(path: Path, *statements: str, parameters: tuple[object, ...] = ()) -> queue.ExpectedControl:
    active = usable(queue.publish_ordinary(path, expected=EMPTY, publication=BINDING))
    sql(path, *statements, parameters=parameters)
    return active


def payload(path: Path) -> dict[str, Any]:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        value: dict[str, Any] = json.loads(
            conn.execute("SELECT payload FROM receipts WHERE seq=1").fetchone()[0]
        )
        return value


def rewrite(path: Path, text: str) -> None:
    sql(path, "UPDATE receipts SET payload=? WHERE seq=1", parameters=(text,))


@pytest.mark.parametrize(
    ("corrupt", "reason"),
    [
        pytest.param(lambda p: sql(p, "UPDATE receipts SET acked=1"), queue.Refusal.ROW_REFUSED, id="acked"),
        pytest.param(lambda p: sql(p, "DELETE FROM receipts"), queue.Refusal.ROW_REFUSED, id="missing"),
        pytest.param(
            lambda p: sql(p, "UPDATE receipts SET action='capture'"),
            queue.Refusal.UNSUPPORTED_ACTION,
            id="capture",
        ),
        pytest.param(
            lambda p: sql(p, "UPDATE receipts SET action='bootstrap'"),
            queue.Refusal.UNSUPPORTED_ACTION,
            id="bootstrap",
        ),
        pytest.param(
            lambda p: sql(p, "UPDATE receipts SET action='capture_abort'"),
            queue.Refusal.UNSUPPORTED_ACTION,
            id="capture-abort",
        ),
        pytest.param(
            lambda p: rewrite(p, json.dumps(payload(p), sort_keys=True)),
            queue.Refusal.ROW_REFUSED,
            id="not-canonical",
        ),
        pytest.param(
            lambda p: rewrite(p, queue._canonical(payload(p) | {"extra": 1})),
            queue.Refusal.ROW_REFUSED,
            id="extra",
        ),
        pytest.param(
            lambda p: rewrite(p, queue._canonical(payload(p) | {"counter": 1})),
            queue.Refusal.ROW_REFUSED,
            id="counter",
        ),
        pytest.param(
            lambda p: rewrite(p, queue._canonical(payload(p) | {"receipt_id": EVIDENCE.receipt_id})),
            queue.Refusal.ROW_REFUSED,
            id="receipt-id",
        ),
        pytest.param(
            lambda p: rewrite(p, queue._canonical(payload(p) | {"boot_id": EVIDENCE.receipt_id})),
            queue.Refusal.ROW_REFUSED,
            id="boot-differs-from-binding",
        ),
        pytest.param(
            lambda p: rewrite(p, '{"counter":0,"counter":0}'), queue.Refusal.ROW_REFUSED, id="duplicate-key"
        ),
        pytest.param(lambda p: rewrite(p, '{"counter":NaN}'), queue.Refusal.ROW_REFUSED, id="nan"),
        pytest.param(lambda p: rewrite(p, "x" * 8193), queue.Refusal.ROW_REFUSED, id="oversized"),
        pytest.param(lambda p: rewrite(p, "[" * 5000), queue.Refusal.ROW_REFUSED, id="deeply-nested"),
        pytest.param(
            lambda p: sql(p, "UPDATE bindings SET server_acked=1"),
            queue.Refusal.ROW_REFUSED,
            id="binding-acked",
        ),
        pytest.param(
            lambda p: sql(p, "DELETE FROM bindings"), queue.Refusal.ROW_REFUSED, id="binding-missing"
        ),
        pytest.param(
            lambda p: sql(p, "UPDATE bindings SET uid=0"), queue.Refusal.ROW_REFUSED, id="binding-uid"
        ),
        pytest.param(
            lambda p: sql(
                p,
                "INSERT INTO capture_aborts(parent_receipt_id,abort_receipt_id) VALUES(?,?)",
                parameters=(RECEIPT, EVIDENCE.receipt_id),
            ),
            queue.Refusal.ROW_REFUSED,
            id="suppressed-parent",
        ),
        pytest.param(
            lambda p: sql(p, "UPDATE identity SET journal_id=NULL"),
            queue.Refusal.ROW_REFUSED,
            id="identity-null",
        ),
        pytest.param(
            lambda p: sql(
                p,
                "UPDATE effective_queue SET generation=?",
                parameters=("40000000-0000-1000-8000-000000000000",),
            ),
            queue.Refusal.ROW_REFUSED,
            id="stored-generation-not-v4",
        ),
    ],
)
def test_a_corrupt_selected_state_refuses_without_a_usable_sender(
    queue_path: Path, corrupt: Any, reason: queue.Refusal
) -> None:
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    corrupt(queue_path)
    before = tables(queue_path)

    for result in (
        queue.observe_ordinary(queue_path, expected=active),
        queue.acknowledge_ordinary(queue_path, expected=active, ack=ack_for(active, RECEIPT, "running")),
        queue.publish_ordinary(queue_path, expected=active, publication=BINDING),
    ):
        refused(result, reason)
    assert tables(queue_path) == before


def test_a_selection_pointing_past_the_receipts_refuses(queue_path: Path) -> None:
    sql(queue_path, "UPDATE effective_queue SET active_seq=99")
    refused(
        queue.observe_ordinary(queue_path, expected=replace(EMPTY, active_seq=99)), queue.Refusal.ROW_REFUSED
    )


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        pytest.param(
            "UPDATE sqlite_sequence SET seq=0", queue.Refusal.ROW_REFUSED, id="sequence-behind-greatest"
        ),
        pytest.param(
            f"UPDATE sqlite_sequence SET seq={MAX}", queue.Refusal.EXHAUSTED, id="sequence-exhausted"
        ),
        pytest.param("DELETE FROM sqlite_sequence", queue.Refusal.ROW_REFUSED, id="sequence-missing"),
    ],
)
def test_inconsistent_or_exhausted_sequence_metadata_refuses_before_dml(
    queue_path: Path, statement: str, reason: queue.Refusal
) -> None:
    empty = bound_and_acked(queue_path)
    sql(queue_path, statement)
    before = tables(queue_path)

    refused(queue.publish_ordinary(queue_path, expected=empty, publication=EVIDENCE), reason)
    assert tables(queue_path) == before


def test_an_exhausted_revision_refuses_mutation_but_not_observation_or_an_identical_match(
    queue_path: Path,
) -> None:
    active = usable(queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING))
    sql(queue_path, f"UPDATE effective_queue SET revision={MAX}")
    at_max = replace(active, revision=MAX)

    assert usable(queue.observe_ordinary(queue_path, expected=at_max)) == at_max
    assert usable(queue.publish_ordinary(queue_path, expected=at_max, publication=BINDING)) == at_max
    refused(
        queue.acknowledge_ordinary(queue_path, expected=at_max, ack=ack_for(at_max, RECEIPT, "running")),
        queue.Refusal.EXHAUSTED,
    )


def test_an_exhausted_counter_refuses_publication(queue_path: Path) -> None:
    sql(queue_path, f"UPDATE identity SET acked_counter={MAX}")
    at_max = replace(EMPTY, acked_counter=MAX)
    refused(queue.publish_ordinary(queue_path, expected=at_max, publication=BINDING), queue.Refusal.EXHAUSTED)


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("PRAGMA user_version=3", id="version-3"),
        pytest.param("PRAGMA user_version=5", id="version-5"),
        pytest.param("CREATE INDEX extra ON receipts(run_id)", id="extra-index"),
        pytest.param("CREATE TABLE extra(x)", id="extra-table"),
        pytest.param("CREATE TRIGGER extra AFTER INSERT ON receipts BEGIN SELECT 1; END", id="trigger"),
        pytest.param("CREATE VIEW extra AS SELECT 1", id="view"),
        pytest.param("ALTER TABLE effective_queue ADD COLUMN extra TEXT", id="mutated-queue"),
        pytest.param("DROP TABLE effective_queue", id="missing-queue"),
        pytest.param("DROP INDEX one_evidence_per_run", id="missing-index"),
    ],
)
def test_any_other_catalog_or_version_refuses_as_schema(queue_path: Path, statement: str) -> None:
    sql(queue_path, statement)
    for result in (
        queue.observe_ordinary(queue_path, expected=EMPTY),
        queue.publish_ordinary(queue_path, expected=EMPTY, publication=BINDING),
    ):
        refused(result, queue.Refusal.SCHEMA_REFUSED)
