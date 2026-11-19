import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night.journal_latch import MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue import (
        ABORT,
        ABORTING,
        CAPTURE,
        CHOICES,
        POLICY,
        PUBLICATION,
        WAKE,
        aborting,
        capture_ack,
        captured,
        proof,
        refused,
        resolution,
        snapshot,
    )
    from packages.agent_board.tests.test_night_effective_queue import (
        CONNECT,
        EMPTY,
        bound_and_acked,
        queue_path,
        tables,
    )
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_capture_queue import (
        ABORT,
        ABORTING,
        CAPTURE,
        CHOICES,
        POLICY,
        PUBLICATION,
        WAKE,
        aborting,
        capture_ack,
        captured,
        proof,
        refused,
        resolution,
        snapshot,
    )
    from test_night_effective_queue import CONNECT, EMPTY, bound_and_acked, queue_path, tables
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
MAX = 2**63 - 1


def sql(path: Path, statement: str, *parameters: object) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute(statement, parameters)


def tamper(value: object, **changes: object) -> Any:
    for name, changed in changes.items():
        object.__setattr__(value, name, changed)
    return value


def corrupt_after(monkeypatch: pytest.MonkeyPatch, prefix: str, statement: str, *parameters: object) -> None:
    original = cq._write

    def anomalous(conn: sqlite3.Connection, text: str, values: tuple[object, ...]) -> sqlite3.Cursor:
        cursor = original(conn, text, values)
        if text.startswith(prefix):
            conn.execute(statement, parameters)
        return cursor

    monkeypatch.setattr(cq, "_write", anomalous)


@pytest.mark.parametrize("outcome", ["aborted", "applied"])
@pytest.mark.parametrize(
    ("column", "value"), [("run_id", "a0000000-0000-4000-8000-000000000000"), ("seq", 42)]
)
def test_resolution_verifies_the_whole_parent_receipt(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, column: str, value: object
) -> None:
    """Regression from the review in board thread 31, post 1909."""
    active = aborting(queue_path)
    before = tables(queue_path)
    corrupt_after(
        monkeypatch,
        "UPDATE capture_aborts",
        f"UPDATE receipts SET {column}=? WHERE receipt_id=?",
        value,
        CAPTURE,
    )

    result = cq.resolve_capture_abort(
        queue_path,
        expected=active,
        claim=resolution(proof(outcome, snapshot() if outcome == "applied" else None)),
    )

    assert (result.fault, result.mutation) == (cq.Fault.SNAPSHOT_FAILURE, MutationOutcome.ROLLED_BACK), result
    assert result.control is None and result.episode is None and result.completion is None
    assert tables(queue_path) == before


def test_abort_publication_verifies_the_retained_parent_sequence(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = captured(queue_path)
    before = tables(queue_path)
    corrupt_after(
        monkeypatch, "INSERT INTO capture_aborts", "UPDATE receipts SET seq=42 WHERE receipt_id=?", CAPTURE
    )

    result = cq.publish_capture_abort(queue_path, expected=active, publication=ABORTING)

    assert result.fault == cq.Fault.SNAPSHOT_FAILURE and result.control is None and result.episode is None
    assert tables(queue_path) == before


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        pytest.param("UPDATE receipts SET seq=0 WHERE receipt_id=?", (CAPTURE,), id="parent-seq-0"),
        pytest.param("UPDATE receipts SET seq=-1 WHERE receipt_id=?", (CAPTURE,), id="parent-seq-negative"),
        pytest.param("UPDATE receipts SET acked=1 WHERE receipt_id=?", (CAPTURE,), id="parent-acked"),
        pytest.param(
            "UPDATE receipts SET action='binding' WHERE receipt_id=?", (CAPTURE,), id="parent-not-capture"
        ),
        pytest.param(
            "UPDATE receipts SET run_id=? WHERE receipt_id=?",
            ("a0000000-0000-4000-8000-000000000000", CAPTURE),
            id="parent-other-run",
        ),
        pytest.param(
            "UPDATE receipts SET payload=replace(payload,'\"counter\":1','\"counter\":0') WHERE receipt_id=?",
            (ABORT,),
            id="abort-payload-differs-from-parent",
        ),
        pytest.param(
            "UPDATE capture_aborts SET outcome='aborted', proof='{}'", (), id="link-already-resolved"
        ),
        pytest.param("UPDATE captures SET snapshot='{}'", (), id="context-already-has-snapshot"),
        pytest.param("UPDATE captures SET cutoff_utc='soon'", (), id="cutoff-stored-as-text"),
        pytest.param(
            "UPDATE captures SET policy=replace(policy,'}',',\"extra\":1}')", (), id="policy-extra-key"
        ),
        pytest.param("UPDATE captures SET principal='pi dev'", (), id="principal-grammar"),
        pytest.param("UPDATE bindings SET server_acked=0", (), id="binding-not-acked"),
    ],
)
def test_a_malformed_selected_abort_refuses_before_any_write(
    queue_path: Path, statement: str, parameters: tuple[object, ...]
) -> None:
    active = aborting(queue_path)
    sql(queue_path, statement, *parameters)
    before = tables(queue_path)

    for result in (
        cq.observe_capture_episode(queue_path, expected=active),
        cq.resolve_capture_abort(queue_path, expected=active, claim=resolution()),
        cq.publish_capture_abort(queue_path, expected=active, publication=ABORTING),
    ):
        refused(result, cq.Refusal.ROW_REFUSED)
    assert tables(queue_path) == before


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "UPDATE captures SET receipt_id='a0000000-0000-4000-8000-000000000000'", id="context-receipt"
        ),
        pytest.param("UPDATE captures SET snapshot='{}', assignment=x'00'", id="context-already-resolved"),
        pytest.param("UPDATE captures SET cutoff_utc=-1.0", id="cutoff-negative"),
        pytest.param("UPDATE captures SET policy='{}'", id="policy-empty"),
        pytest.param(
            f"INSERT INTO capture_aborts(parent_receipt_id,abort_receipt_id) VALUES('{CAPTURE}','{ABORT}')",
            id="capture-already-linked",
        ),
    ],
)
def test_a_malformed_selected_capture_refuses_before_any_write(queue_path: Path, statement: str) -> None:
    active = captured(queue_path)
    sql(queue_path, statement)
    before = tables(queue_path)

    for result in (
        cq.observe_capture_episode(queue_path, expected=active),
        cq.acknowledge_capture(queue_path, expected=active, claim=capture_ack()),
        cq.publish_capture(queue_path, expected=active, publication=PUBLICATION),
    ):
        refused(result, cq.Refusal.ROW_REFUSED)
    assert tables(queue_path) == before


@pytest.mark.parametrize("key", ["id", "thread_id", "topic_id"])
@pytest.mark.parametrize("operation", ["capture", "applied"])
def test_wake_identifiers_are_bounded_to_signed64_before_sql(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, operation: str
) -> None:
    active = captured(queue_path) if operation == "capture" else aborting(queue_path)
    value = snapshot(wakes=[WAKE | {key: 2**63}])
    tape = Tape(monkeypatch)
    if operation == "capture":
        result = cq.acknowledge_capture(queue_path, expected=active, claim=capture_ack(value))
    else:
        result = cq.resolve_capture_abort(
            queue_path, expected=active, claim=resolution(proof("applied", value))
        )
    tape.dispose()
    refused(result, cq.Refusal.CAPTURE_UNUSABLE)
    assert tape.attempts == [] and result.mutation == MutationOutcome.NOT_ATTEMPTED


def test_a_wake_identifier_at_the_signed64_maximum_is_usable(queue_path: Path) -> None:
    active = captured(queue_path)
    result = cq.acknowledge_capture(
        queue_path, expected=active, claim=capture_ack(snapshot(wakes=[WAKE | {"id": MAX}]))
    )
    assert result.refusal is None and result.completion is not None, result


class Text(str):
    pass


class Number(int):
    pass


class Real(float):
    pass


@pytest.mark.parametrize(
    "publication",
    [
        *[
            pytest.param(
                replace(
                    PUBLICATION,
                    choices=replace(
                        CHOICES,
                        policy=tamper(
                            replace(POLICY),
                            **{key: Text("fixture-model" if key == "model" else getattr(POLICY, key))},
                        ),
                    ),
                ),
                id=f"policy-{key}-subclass",
            )
            for key in ("repository_key", "base_commit", "backend", "model", "permission_mode")
        ],
        pytest.param(
            replace(
                PUBLICATION,
                choices=replace(CHOICES, policy=tamper(replace(POLICY), turn_seconds=Number(600))),
            ),
            id="policy-turn-seconds-subclass",
        ),
        pytest.param(
            replace(PUBLICATION, choices=replace(CHOICES, policy=tamper(replace(POLICY), turn_seconds=True))),
            id="policy-turn-seconds-bool",
        ),
        pytest.param(
            replace(PUBLICATION, choices=replace(CHOICES, principal=Text("pi@dev"))), id="principal-subclass"
        ),
        pytest.param(
            replace(PUBLICATION, choices=replace(CHOICES, principal="p" * 125 + "@dev")), id="principal-long"
        ),
        pytest.param(replace(PUBLICATION, choices=replace(CHOICES, cutoff_utc=2000)), id="cutoff-int"),
        pytest.param(
            replace(PUBLICATION, choices=replace(CHOICES, cutoff_utc=Real(2000.0))), id="cutoff-subclass"
        ),
        pytest.param(
            replace(PUBLICATION, choices=replace(CHOICES, cutoff_utc=float("inf"))), id="cutoff-inf"
        ),
        pytest.param(replace(PUBLICATION, choices=replace(CHOICES, cutoff_utc=0.0)), id="cutoff-zero"),
        pytest.param(tamper(replace(PUBLICATION), choices=object()), id="choices-object"),
    ],
)
def test_a_malformed_capture_publication_is_invalid_input_before_sql(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, publication: cq.CapturePublication
) -> None:
    empty = bound_and_acked(queue_path)
    tape = Tape(monkeypatch)
    result = cq.publish_capture(queue_path, expected=empty, publication=publication)
    tape.dispose()
    refused(result, cq.Refusal.INVALID_INPUT)
    assert tape.attempts == []


class Uncopyable:
    def __deepcopy__(self, memo: object) -> object:
        raise AssertionError("unvalidated input was copied")


UNCOPYABLE: Any = Uncopyable()
BYTEARRAY: Any = bytearray(b"{}")


@pytest.mark.parametrize(
    "argument",
    [
        pytest.param(("abort", replace(ABORTING, receipt_id=UNCOPYABLE)), id="abort-uncopyable-field"),
        pytest.param(("abort", replace(ABORTING, receipt_id=CAPTURE)), id="abort-id-equals-parent"),
        pytest.param(("abort", object.__new__(cq.CaptureAbortPublication)), id="abort-uninitialized"),
        pytest.param(("ack", cq.CaptureAckClaim(CAPTURE, BYTEARRAY)), id="ack-packet-bytearray"),
        pytest.param(("ack", object.__new__(cq.CaptureAckClaim)), id="ack-uninitialized"),
        pytest.param(("resolve", cq.AbortResolutionClaim(Text(ABORT), b"{}")), id="resolve-receipt-subclass"),
    ],
)
def test_malformed_abort_and_claim_inputs_are_invalid_input(
    argument: tuple[str, Any], queue_path: Path
) -> None:
    kind, value = argument
    if kind == "abort":
        result = cq.publish_capture_abort(queue_path, expected=EMPTY, publication=value)
    elif kind == "ack":
        result = cq.acknowledge_capture(queue_path, expected=EMPTY, claim=value)
    else:
        result = cq.resolve_capture_abort(queue_path, expected=EMPTY, claim=value)
    refused(result, cq.Refusal.INVALID_INPUT)


@pytest.mark.parametrize(
    ("prepare", "statement", "operation"),
    [
        pytest.param(
            bound_and_acked,
            f"UPDATE effective_queue SET revision={MAX}",
            lambda p, c: cq.publish_capture(p, expected=c, publication=PUBLICATION),
            id="publish-capture-revision",
        ),
        pytest.param(
            bound_and_acked,
            f"UPDATE identity SET acked_counter={MAX}",
            lambda p, c: cq.publish_capture(p, expected=c, publication=PUBLICATION),
            id="publish-capture-counter",
        ),
        pytest.param(
            bound_and_acked,
            f"UPDATE sqlite_sequence SET seq={MAX}",
            lambda p, c: cq.publish_capture(p, expected=c, publication=PUBLICATION),
            id="publish-capture-sequence",
        ),
        pytest.param(
            captured,
            f"UPDATE effective_queue SET revision={MAX}",
            lambda p, c: cq.acknowledge_capture(p, expected=c, claim=capture_ack()),
            id="acknowledge-revision",
        ),
        pytest.param(
            captured,
            f"UPDATE sqlite_sequence SET seq={MAX}",
            lambda p, c: cq.publish_capture_abort(p, expected=c, publication=ABORTING),
            id="publish-abort-sequence",
        ),
        pytest.param(
            aborting,
            f"UPDATE effective_queue SET revision={MAX}",
            lambda p, c: cq.resolve_capture_abort(p, expected=c, claim=resolution()),
            id="resolve-revision",
        ),
    ],
)
def test_exhaustion_refuses_before_any_write(
    queue_path: Path, prepare: Any, statement: str, operation: Any
) -> None:
    control = prepare(queue_path)
    sql(queue_path, statement)
    if "revision" in statement:
        control = replace(control, revision=MAX)
    if "acked_counter" in statement:
        control = replace(control, acked_counter=MAX)
    before = tables(queue_path)
    refused(operation(queue_path, control), cq.Refusal.EXHAUSTED)
    assert tables(queue_path) == before


def test_observation_and_an_identical_match_still_work_at_the_maximum_revision(queue_path: Path) -> None:
    active = aborting(queue_path)
    sql(queue_path, f"UPDATE effective_queue SET revision={MAX}")
    at_max = replace(active, revision=MAX)
    assert cq.observe_capture_episode(queue_path, expected=at_max).control == at_max
    assert cq.publish_capture_abort(queue_path, expected=at_max, publication=ABORTING).control == at_max


OVERFLOW = '{"capture_counter":1,"counter":1e999}'


@pytest.mark.parametrize(
    ("operation", "raw"),
    [
        pytest.param(
            "capture", cq._canonical(snapshot()).replace('"deadline":1900', '"deadline":1e999'), id="deadline"
        ),
        pytest.param(
            "applied",
            cq._canonical(proof("applied", snapshot())).replace('"deadline":1900', '"deadline":1e999'),
            id="applied-deadline",
        ),
        pytest.param(
            "aborted", cq._canonical(proof()).replace('"counter":2', '"counter":1e999'), id="proof-counter"
        ),
        pytest.param(
            "capture",
            cq._canonical(snapshot()).replace('"counter":2', '"counter":' + "9" * 5000),
            id="huge-int",
        ),
        pytest.param("capture", "[" * 40000 + "]" * 40000, id="over-depth"),
        pytest.param("aborted", '{"a":' * 40000 + "1" + "}" * 40000, id="over-depth-object"),
    ],
)
def test_overflowing_or_over_deep_packets_are_unusable_before_sql(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, raw: str
) -> None:
    active = captured(queue_path) if operation == "capture" else aborting(queue_path)
    tape = Tape(monkeypatch)
    if operation == "capture":
        result = cq.acknowledge_capture(
            queue_path, expected=active, claim=cq.CaptureAckClaim(CAPTURE, raw.encode())
        )
    else:
        result = cq.resolve_capture_abort(
            queue_path, expected=active, claim=cq.AbortResolutionClaim(ABORT, raw.encode())
        )
    tape.dispose()
    refused(result, cq.Refusal.CAPTURE_UNUSABLE)
    assert result.fault is None and tape.attempts == []


@pytest.mark.parametrize("episode", ["capture", "abort"])
@pytest.mark.parametrize(
    ("table", "column", "old", "new"),
    [
        ("receipts", "payload", '"counter":1', '"counter":1e999'),
        ("captures", "policy", '"turn_seconds":600', '"turn_seconds":1e999'),
    ],
)
def test_overflowing_stored_numbers_are_row_refusals_not_file_faults(
    queue_path: Path, episode: str, table: str, column: str, old: str, new: str
) -> None:
    active = captured(queue_path) if episode == "capture" else aborting(queue_path)
    sql(
        queue_path, f"UPDATE {table} SET {column}=replace({column},?,?) WHERE receipt_id=?", old, new, CAPTURE
    )
    refused(cq.observe_capture_episode(queue_path, expected=active), cq.Refusal.ROW_REFUSED)


def test_an_overflowing_value_written_during_publication_is_a_snapshot_fault(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = bound_and_acked(queue_path)
    before = tables(queue_path)
    corrupt_after(
        monkeypatch,
        "INSERT INTO captures",
        "UPDATE receipts SET payload=replace(payload,'\"counter\":1','\"counter\":1e999') WHERE receipt_id=?",
        CAPTURE,
    )
    result = cq.publish_capture(queue_path, expected=empty, publication=PUBLICATION)
    assert result.fault == cq.Fault.SNAPSHOT_FAILURE and result.control is None
    assert tables(queue_path) == before


@pytest.mark.parametrize(
    ("operation", "raw"),
    [
        pytest.param("capture", "[" * 3000 + "]" * 3000, id="capture-array-depth-under-cap"),
        pytest.param("capture", '{"a":' * 3000 + "1" + "}" * 3000, id="capture-object-depth-under-cap"),
        pytest.param("aborted", "[" * 3000 + "]" * 3000, id="proof-array-depth-under-cap"),
        pytest.param("aborted", '{"a":' * 3000 + "1" + "}" * 3000, id="proof-object-depth-under-cap"),
    ],
)
def test_over_deep_packets_within_their_byte_caps_are_unusable_before_sql(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, raw: str
) -> None:
    assert len(raw) < cq.SNAPSHOT_MAX
    active = captured(queue_path) if operation == "capture" else aborting(queue_path)
    tape = Tape(monkeypatch)
    if operation == "capture":
        result = cq.acknowledge_capture(
            queue_path, expected=active, claim=cq.CaptureAckClaim(CAPTURE, raw.encode())
        )
    else:
        result = cq.resolve_capture_abort(
            queue_path, expected=active, claim=cq.AbortResolutionClaim(ABORT, raw.encode())
        )
    tape.dispose()
    refused(result, cq.Refusal.CAPTURE_UNUSABLE)
    assert result.fault is None and tape.attempts == []


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        pytest.param("receipts", "payload", "[" * 4000 + "]" * 4000, id="receipt-payload-depth-under-cap"),
        pytest.param("captures", "policy", "[" * 1000 + "]" * 1000, id="policy-depth-under-cap"),
    ],
)
def test_over_deep_stored_rows_within_their_caps_are_row_refusals(
    queue_path: Path, table: str, column: str, value: str
) -> None:
    active = captured(queue_path)
    sql(queue_path, f"UPDATE {table} SET {column}=? WHERE receipt_id=?", value, CAPTURE)
    refused(cq.observe_capture_episode(queue_path, expected=active), cq.Refusal.ROW_REFUSED)
