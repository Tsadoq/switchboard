import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from agent_board.night import effective_queue as eq
from agent_board.night.assignment import (
    MAX_ASSIGNMENT_BYTES,
    AssignmentError,
    NightPolicy,
    _check_choices,
    build_assignment,
    encode,
    parse_wakes,
)
from agent_board.night.effective_queue import MAX_INT, ExpectedControl
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.ledger_binding import BindingClaim
from agent_board.night_state import StateError, check_private_file

REQUEST_MAX = 8192
POLICY_MAX = 2048
SNAPSHOT_MAX = 65536
PROOF_MAX = 131072
PRINCIPAL_MAX = 128
TEXT_MAX = 32768
PRINCIPAL = re.compile(r"[A-Za-z0-9._-]+@[a-z0-9_-]+")
_REQUEST_FIELDS = frozenset(
    ("fence", "journal_id", "counter", "receipt_id", "boot_id", "invocation_id", "cgroup")
)
_ABORT_FIELDS = _REQUEST_FIELDS | {"capture_receipt_id", "capture_counter"}
_SNAPSHOT_FIELDS = frozenset(
    ("journal_id", "counter", "capture_id", "run_id", "principal", "deadline", "wakes")
)
_PROOF_FIELDS = frozenset(
    ("journal_id", "run_id", "abort_id", "capture_id", "capture_counter", "counter", "outcome", "capture")
)
_POLICY_FIELDS = frozenset(NightPolicy.__dataclass_fields__)
_POLICY_TEXT = ("repository_key", "base_commit", "backend", "permission_mode")


class Refusal(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    SIDECAR_PRESENT = "SIDECAR_PRESENT"
    BUSY = "BUSY"
    SCHEMA_REFUSED = "SCHEMA_REFUSED"
    ROW_REFUSED = "ROW_REFUSED"
    LEDGER_IDENTITY_MISMATCH = "LEDGER_IDENTITY_MISMATCH"
    CONTROL_MISMATCH = "CONTROL_MISMATCH"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    PUBLICATION_CONFLICT = "PUBLICATION_CONFLICT"
    RECEIPT_CONFLICT = "RECEIPT_CONFLICT"
    BINDING_CONFLICT = "BINDING_CONFLICT"
    BINDING_ALREADY_ACKED = "BINDING_ALREADY_ACKED"
    BINDING_NOT_ACKED = "BINDING_NOT_ACKED"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    NOTHING_SELECTED = "NOTHING_SELECTED"
    ACK_REFUSED = "ACK_REFUSED"
    EXHAUSTED = "EXHAUSTED"
    CAPTURE_ALREADY_EXISTS = "CAPTURE_ALREADY_EXISTS"
    CAPTURE_UNUSABLE = "CAPTURE_UNUSABLE"
    WRONG_ACTION = "WRONG_ACTION"


Fault = eq.Fault


@dataclass(frozen=True, slots=True)
class CaptureChoices:
    principal: str
    cutoff_utc: float
    policy: NightPolicy


@dataclass(frozen=True, slots=True)
class CapturePublication:
    receipt_id: str
    run_id: str
    fence: str
    claim: BindingClaim
    choices: CaptureChoices


@dataclass(frozen=True, slots=True)
class CaptureAbortPublication:
    receipt_id: str
    run_id: str
    capture_receipt_id: str


@dataclass(frozen=True, slots=True)
class CaptureAckClaim:
    receipt_id: str
    packet: bytes


@dataclass(frozen=True, slots=True)
class AbortResolutionClaim:
    receipt_id: str
    packet: bytes


@dataclass(frozen=True, slots=True)
class CaptureEpisode:
    seq: int
    action: Literal["capture", "capture_abort"]
    run_id: str
    capture_seq: int
    capture_receipt_id: str
    capture_payload: str
    choices: CaptureChoices
    abort_receipt_id: str | None
    abort_payload: str | None


@dataclass(frozen=True, slots=True)
class CapturedCompletion:
    snapshot: str
    assignment: bytes | None


@dataclass(frozen=True, slots=True)
class AbortedCompletion:
    proof: str


@dataclass(frozen=True, slots=True)
class AppliedAfterAbortCompletion:
    snapshot: str
    proof: str


type Completion = CapturedCompletion | AbortedCompletion | AppliedAfterAbortCompletion


@dataclass(frozen=True, slots=True)
class CaptureResult:
    mutation: MutationOutcome
    cleanup: CleanupOutcome
    changed: bool | None
    control: ExpectedControl | None
    episode: CaptureEpisode | None
    completion: Completion | None
    refusal: Refusal | None
    fault: eq.Fault | None


class _Refused(ValueError):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class _Attempt(eq._Attempt):
    capture_refusal: Refusal | None = None
    episode: CaptureEpisode | None = None
    completion: Completion | None = field(default=None)

    def outcome(self) -> CaptureResult:
        usable = (
            self.finished
            and self.cleanup == CleanupOutcome.CLOSED
            and self.capture_refusal is None
            and self.fault is None
            and self.mutation in (MutationOutcome.COMMITTED, MutationOutcome.ROLLED_BACK)
        )
        changed = self.changed if self.mutation == MutationOutcome.COMMITTED else False
        return CaptureResult(
            self.mutation,
            self.cleanup,
            None if self.mutation == MutationOutcome.UNKNOWN else changed,
            self.control if usable else None,
            self.episode if usable else None,
            self.completion if usable else None,
            self.capture_refusal,
            self.fault,
        )


def _require(condition: bool, refusal: Refusal = Refusal.ROW_REFUSED) -> None:
    if not condition:
        raise _Refused(refusal)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _strict(text: str, refusal: Refusal) -> dict[str, Any]:
    """Strict canonical JSON: an exponent overflowing to infinity is refused, never decoded as a float."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        _require(len({key for key, _ in items}) == len(items), refusal)
        return dict(items)

    def constant(name: str) -> None:
        raise _Refused(refusal)

    def number(literal: str) -> float:
        value = float(literal)
        _require(math.isfinite(value), refusal)
        return value

    try:
        value: object = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
        _require(type(value) is dict and _canonical(value) == text, refusal)
    except (ValueError, RecursionError):
        raise _Refused(refusal) from None
    assert isinstance(value, dict)
    return value


def _choices(value: object) -> CaptureChoices:
    bad = Refusal.INVALID_INPUT
    _require(type(value) is CaptureChoices, bad)
    assert isinstance(value, CaptureChoices)
    principal, cutoff, policy = value.principal, value.cutoff_utc, value.policy
    _require(type(principal) is str and len(principal) <= PRINCIPAL_MAX, bad)
    _require(principal.isascii() and PRINCIPAL.fullmatch(principal) is not None, bad)
    _require(type(cutoff) is float and math.isfinite(cutoff) and cutoff > 0, bad)
    _require(type(policy) is NightPolicy, bad)
    fields = tuple(getattr(policy, name) for name in NightPolicy.__dataclass_fields__)
    model = policy.model
    _require(all(type(getattr(policy, name)) is str for name in _POLICY_TEXT), bad)
    _require(model is None or type(model) is str, bad)
    _require(type(policy.turn_seconds) is int, bad)
    try:
        _check_choices(*fields)
    except AssignmentError:
        raise _Refused(bad) from None
    checked = NightPolicy(*fields)
    _require(len(_policy_text(checked)) <= POLICY_MAX, bad)
    return CaptureChoices(principal, cutoff, checked)


def _policy_text(policy: NightPolicy) -> str:
    return _canonical(asdict(policy))


def _publication(value: object) -> CapturePublication:
    bad = Refusal.INVALID_INPUT
    _require(type(value) is CapturePublication, bad)
    assert isinstance(value, CapturePublication)
    try:
        claim = eq._claim(value.claim, eq.Refusal.INVALID_INPUT)
        receipt_id, run_id = (
            eq._uuid(value.receipt_id, eq.Refusal.INVALID_INPUT),
            eq._uuid(value.run_id, eq.Refusal.INVALID_INPUT),
        )
        fence = eq._fence(value.fence, eq.Refusal.INVALID_INPUT)
    except eq._Refused:
        raise _Refused(bad) from None
    _require(claim.run_id == run_id, bad)
    return CapturePublication(receipt_id, run_id, fence, claim, _choices(value.choices))


def _abort_publication(value: object) -> CaptureAbortPublication:
    bad = Refusal.INVALID_INPUT
    _require(type(value) is CaptureAbortPublication, bad)
    assert isinstance(value, CaptureAbortPublication)
    try:
        ids = [
            eq._uuid(item, eq.Refusal.INVALID_INPUT)
            for item in (value.receipt_id, value.run_id, value.capture_receipt_id)
        ]
    except eq._Refused:
        raise _Refused(bad) from None
    _require(ids[0] != ids[2], bad)
    return CaptureAbortPublication(*ids)


def _packet(value: object, kind: type[CaptureAckClaim] | type[AbortResolutionClaim]) -> tuple[str, bytes]:
    bad = Refusal.INVALID_INPUT
    _require(type(value) is kind, bad)
    assert isinstance(value, CaptureAckClaim | AbortResolutionClaim)
    try:
        receipt_id = eq._uuid(value.receipt_id, eq.Refusal.INVALID_INPUT)
    except eq._Refused:
        raise _Refused(bad) from None
    _require(type(value.packet) is bytes, bad)
    return receipt_id, value.packet


def _decode(packet: bytes, cap: int) -> dict[str, Any]:
    unusable = Refusal.CAPTURE_UNUSABLE
    _require(len(packet) <= cap and packet.isascii(), unusable)
    return _strict(packet.decode("ascii"), unusable)


def _snapshot(value: object) -> dict[str, Any]:
    unusable = Refusal.CAPTURE_UNUSABLE
    _require(type(value) is dict, unusable)
    assert isinstance(value, dict)
    _require(frozenset(value) == _SNAPSHOT_FIELDS, unusable)
    for key in ("journal_id", "capture_id", "run_id"):
        try:
            eq._uuid(value[key], eq.Refusal.ROW_REFUSED)
        except eq._Refused:
            raise _Refused(unusable) from None
    counter, deadline, principal = value["counter"], value["deadline"], value["principal"]
    _require(type(counter) is int and 1 <= counter <= MAX_INT, unusable)
    _require(type(principal) is str and len(principal) <= PRINCIPAL_MAX, unusable)
    _require(principal.isascii() and PRINCIPAL.fullmatch(principal) is not None, unusable)
    if type(deadline) is int:
        _require(1 <= deadline <= MAX_INT, unusable)
    else:
        _require(type(deadline) is float and math.isfinite(deadline) and deadline > 0, unusable)
    wakes = value["wakes"]
    _require(type(wakes) is list, unusable)
    for wake in wakes:
        _require(type(wake) is dict, unusable)
        for key in ("id", "thread_id", "topic_id"):
            number = wake.get(key)
            _require(number is None or type(number) is not int or number <= MAX_INT, unusable)
        for key in ("reason", "source_ref"):
            text = wake.get(key)
            _require(text is None or (type(text) is str and len(text) <= TEXT_MAX), unusable)
    try:
        parse_wakes(wakes)
    except AssignmentError:
        raise _Refused(unusable) from None
    return value


def _snapshot_packet(packet: bytes) -> tuple[str, dict[str, Any]]:
    value = _snapshot(_decode(packet, SNAPSHOT_MAX))
    return packet.decode("ascii"), value


def _proof_packet(packet: bytes) -> tuple[str, dict[str, Any], tuple[str, dict[str, Any]] | None]:
    unusable = Refusal.CAPTURE_UNUSABLE
    proof = _decode(packet, PROOF_MAX)
    _require(frozenset(proof) == _PROOF_FIELDS, unusable)
    for key in ("journal_id", "run_id", "abort_id", "capture_id"):
        try:
            eq._uuid(proof[key], eq.Refusal.ROW_REFUSED)
        except eq._Refused:
            raise _Refused(unusable) from None
    for key, low in (("capture_counter", 0), ("counter", 1)):
        _require(type(proof[key]) is int and low <= proof[key] <= MAX_INT, unusable)
    outcome, capture = proof["outcome"], proof["capture"]
    _require((outcome, capture is None) in (("aborted", True), ("applied", False)), unusable)
    if capture is None:
        return packet.decode("ascii"), proof, None
    snapshot = _snapshot(capture)
    try:
        text = _canonical(snapshot)
    except (ValueError, RecursionError):
        raise _Refused(unusable) from None
    _require(len(text) <= SNAPSHOT_MAX, unusable)
    return packet.decode("ascii"), proof, (text, snapshot)


def _derive(snapshot: dict[str, Any], run_id: str, choices: CaptureChoices) -> bytes | None:
    try:
        assignment = build_assignment(
            run_id=run_id,
            principal=choices.principal,
            wakes=parse_wakes(snapshot["wakes"]),
            policy=choices.policy,
            deadline_utc=min(float(snapshot["deadline"]), choices.cutoff_utc),
        )
        encoded = None if assignment is None else encode(assignment)
    except (AssignmentError, ValueError, TypeError, OverflowError, RecursionError):
        raise _Refused(Refusal.CAPTURE_UNUSABLE) from None
    _require(encoded is None or len(encoded) <= MAX_ASSIGNMENT_BYTES, Refusal.CAPTURE_UNUSABLE)
    return encoded


def _request(publication: CapturePublication, expected: ExpectedControl) -> str:
    claim = publication.claim
    text = _canonical(
        {
            "fence": publication.fence,
            "journal_id": expected.journal_id,
            "counter": expected.acked_counter,
            "receipt_id": publication.receipt_id,
            "boot_id": claim.boot_id,
            "invocation_id": claim.invocation_id,
            "cgroup": claim.cgroup,
        }
    )
    _require(len(text) <= REQUEST_MAX, Refusal.INVALID_INPUT)
    return text


def _abort_request(parent_payload: str, abort_receipt_id: str, capture_receipt_id: str) -> str:
    payload = json.loads(parent_payload)
    payload |= {
        "receipt_id": abort_receipt_id,
        "capture_receipt_id": capture_receipt_id,
        "capture_counter": payload["counter"],
    }
    return _canonical(payload)


_CAPTURES = {
    "run_id": ("text", 36),
    "receipt_id": ("text", 36),
    "principal": ("text", PRINCIPAL_MAX),
    "cutoff_utc": ("real", 0),
    "policy": ("text", POLICY_MAX),
    "snapshot": ("text", SNAPSHOT_MAX),
    "assignment": ("blob", MAX_ASSIGNMENT_BYTES),
}
_LINK = {
    "parent_receipt_id": ("text", 36),
    "abort_receipt_id": ("text", 36),
    "outcome": ("text", 16),
    "proof": ("text", PROOF_MAX),
}
CAPTURE_PER_RUN_QUERY = (
    "SELECT seq FROM receipts INDEXED BY one_capture_per_run WHERE run_id=? AND action='capture' LIMIT 2"
)
LINK_BY_PARENT_QUERY = f"SELECT {eq._projection(_LINK)} FROM capture_aborts WHERE parent_receipt_id=? LIMIT 2"
LINK_BY_ABORT_QUERY = (
    f"SELECT {eq._projection(_LINK)} FROM capture_aborts "
    "INDEXED BY sqlite_autoindex_capture_aborts_2 WHERE abort_receipt_id=? LIMIT 2"
)
RECEIPT_BY_ID_QUERY = (
    f"SELECT {eq._projection(eq._RECEIPT)} FROM receipts INDEXED BY sqlite_autoindex_receipts_1 "
    "WHERE receipt_id=? LIMIT 2"
)


def _row(
    row: sqlite3.Row, spec: dict[str, tuple[str, int]], nullable: frozenset[str] = frozenset()
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, (kind, cap) in spec.items():
        value, actual = row[name], row[f"{name}_kind"]
        if actual == "null" and name in nullable:
            result[name] = None
            continue
        _require(actual == kind)
        if kind == "real":
            _require(type(value) is float)
            result[name] = value
        elif kind == "blob":
            _require(type(value) is bytes and len(value) <= cap)
            result[name] = value
        elif cap:
            _require(type(value) is bytes and len(value) <= cap)
            try:
                result[name] = value.decode("utf-8")
            except UnicodeError:
                raise _Refused(Refusal.ROW_REFUSED) from None
        else:
            _require(type(value) is int)
            result[name] = value
    return result


def _projection_real(spec: dict[str, tuple[str, int]]) -> str:
    columns: list[str] = []
    for name, (kind, cap) in spec.items():
        if kind == "real":
            value = f"CASE WHEN typeof({name})='real' THEN {name} END"
        elif kind == "integer" or not cap:
            value = f"CASE WHEN typeof({name})='{kind}' THEN {name} END"
        else:
            value = f"substr(CAST({name} AS BLOB),1,{cap + 1})"
        columns.extend((f"{value} AS {name}", f"typeof({name}) AS {name}_kind"))
    return ",".join(columns)


CAPTURES_QUERY = f"SELECT {_projection_real(_CAPTURES)} FROM captures WHERE run_id=? LIMIT 2"


def _strict_row_json(text: str) -> dict[str, Any]:
    return _strict(text, Refusal.ROW_REFUSED)


def _receipt(rows: list[sqlite3.Row]) -> dict[str, Any]:
    _require(len(rows) == 1)
    try:
        return eq._row(rows[0], eq._RECEIPT)
    except eq._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None


def _capture_payload(text: str, control: ExpectedControl, receipt_id: str) -> dict[str, Any]:
    payload = _strict_row_json(text)
    _require(frozenset(payload) == _REQUEST_FIELDS)
    _require(payload["journal_id"] == control.journal_id and payload["receipt_id"] == receipt_id)
    _require(type(payload["counter"]) is int and payload["counter"] == control.acked_counter)
    try:
        eq._fence(payload["fence"])
    except eq._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None
    return payload


def _context(
    conn: sqlite3.Connection, run_id: str, receipt_id: str, *, resolved: bool = False
) -> tuple[CaptureChoices, dict[str, Any]]:
    rows = conn.execute(CAPTURES_QUERY, (run_id,)).fetchall()
    _require(len(rows) == 1)
    row = _row(rows[0], _CAPTURES, frozenset(("snapshot", "assignment")))
    _require(row["run_id"] == run_id and row["receipt_id"] == receipt_id)
    if not resolved:
        _require(row["snapshot"] is None and row["assignment"] is None)
    policy = _strict_row_json(row["policy"])
    _require(frozenset(policy) == _POLICY_FIELDS)
    try:
        choices = _choices(CaptureChoices(row["principal"], row["cutoff_utc"], NightPolicy(**policy)))
    except (_Refused, AssignmentError, TypeError):
        raise _Refused(Refusal.ROW_REFUSED) from None
    _require(_policy_text(choices.policy) == row["policy"])
    return choices, row


def _bound(conn: sqlite3.Connection, run_id: str, payload: dict[str, Any]) -> BindingClaim:
    try:
        stored = eq._binding(conn, run_id)
    except eq._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None
    _require(stored is not None and stored[1])
    assert stored is not None
    claim = stored[0]
    _require(
        (payload["boot_id"], payload["invocation_id"], payload["cgroup"])
        == (claim.boot_id, claim.invocation_id, claim.cgroup)
    )
    return claim


def _episode(conn: sqlite3.Connection, control: ExpectedControl) -> tuple[CaptureEpisode, BindingClaim]:
    seq = control.active_seq
    if seq is None:
        raise _Refused(Refusal.NOTHING_SELECTED)
    selected = _receipt(conn.execute(eq.SELECTED_QUERY, (seq,)).fetchall())
    action = selected["action"]
    _require(action in ("capture", "capture_abort"), Refusal.WRONG_ACTION)
    _require(selected["seq"] == seq and selected["acked"] == 0)
    try:
        run_id, receipt_id = eq._uuid(selected["run_id"]), eq._uuid(selected["receipt_id"])
    except eq._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None
    if action == "capture":
        _require(conn.execute(LINK_BY_PARENT_QUERY, (receipt_id,)).fetchall() == [])
        payload = _capture_payload(selected["payload"], control, receipt_id)
        claim = _bound(conn, run_id, payload)
        choices, _ = _context(conn, run_id, receipt_id)
        return CaptureEpisode(
            seq, "capture", run_id, seq, receipt_id, selected["payload"], choices, None, None
        ), claim
    links = conn.execute(LINK_BY_ABORT_QUERY, (receipt_id,)).fetchall()
    _require(len(links) == 1)
    link = _row(links[0], _LINK, frozenset(("outcome", "proof")))
    parent_id = link["parent_receipt_id"]
    _require(link["abort_receipt_id"] == receipt_id and link["outcome"] is None and link["proof"] is None)
    _require(parent_id != receipt_id)
    parent = _receipt(conn.execute(RECEIPT_BY_ID_QUERY, (parent_id,)).fetchall())
    _require(parent["action"] == "capture" and parent["acked"] == 0 and parent["run_id"] == run_id)
    _require(parent["receipt_id"] == parent_id and 1 <= parent["seq"] <= MAX_INT and parent["seq"] != seq)
    payload = _capture_payload(parent["payload"], control, parent_id)
    abort_text = selected["payload"]
    _strict_row_json(abort_text)
    _require(abort_text == _abort_request(parent["payload"], receipt_id, parent_id))
    claim = _bound(conn, run_id, payload)
    choices, _ = _context(conn, run_id, parent_id)
    episode = CaptureEpisode(
        seq,
        "capture_abort",
        run_id,
        parent["seq"],
        parent_id,
        parent["payload"],
        choices,
        receipt_id,
        abort_text,
    )
    return episode, claim


def _after(
    conn: sqlite3.Connection, after: ExpectedControl, episode: CaptureEpisode, claim: BindingClaim
) -> None:
    try:
        if eq._read_control(conn) != after:
            raise eq._Failed(Fault.SNAPSHOT_FAILURE)
        observed = _episode(conn, after)
    except (eq._Refused, _Refused):
        raise eq._Failed(Fault.SNAPSHOT_FAILURE) from None
    if observed != (episode, claim):
        raise eq._Failed(Fault.SNAPSHOT_FAILURE)


def _write(conn: sqlite3.Connection, statement: str, parameters: tuple[object, ...]) -> sqlite3.Cursor:
    cursor = conn.execute(statement, parameters)
    if cursor.rowcount != 1:
        raise eq._Failed(Fault.SNAPSHOT_FAILURE)
    return cursor


def _advance(conn: sqlite3.Connection, expected: ExpectedControl, active: int | None) -> ExpectedControl:
    if active is None:
        _write(
            conn,
            "UPDATE effective_queue SET revision=?,active_seq=NULL WHERE singleton=1 AND revision=? AND active_seq=?",
            (expected.revision + 1, expected.revision, expected.active_seq),
        )
    elif expected.active_seq is None:
        _write(
            conn,
            "UPDATE effective_queue SET revision=?,active_seq=? WHERE singleton=1 AND revision=? AND active_seq IS NULL",
            (expected.revision + 1, active, expected.revision),
        )
    else:
        _write(
            conn,
            "UPDATE effective_queue SET revision=?,active_seq=? WHERE singleton=1 AND revision=? AND active_seq=?",
            (expected.revision + 1, active, expected.revision, expected.active_seq),
        )
    return ExpectedControl(
        expected.journal_id, expected.generation, expected.revision + 1, active, expected.acked_counter
    )


def _publish_capture(
    conn: sqlite3.Connection, attempt: _Attempt, expected: ExpectedControl, publication: CapturePublication
) -> None:
    payload = _request(publication, expected)
    policy = _policy_text(publication.choices.policy)
    if expected.active_seq is not None:
        episode, claim = _episode(conn, expected)
        _require(episode.action == "capture", Refusal.WRONG_ACTION)
        _require(
            (episode.capture_receipt_id, episode.run_id, episode.capture_payload, episode.choices, claim)
            == (publication.receipt_id, publication.run_id, payload, publication.choices, publication.claim),
            Refusal.PUBLICATION_CONFLICT,
        )
        attempt.control, attempt.episode = expected, episode
        eq._rollback(conn, attempt)
        return
    _require(expected.revision < MAX_INT and expected.acked_counter < MAX_INT, Refusal.EXHAUSTED)
    try:
        stored = eq._binding(conn, publication.run_id)
    except eq._Refused:
        raise _Refused(Refusal.ROW_REFUSED) from None
    if stored is not None:
        _require(stored[0] == publication.claim, Refusal.BINDING_CONFLICT)
    _require(stored is not None and stored[1], Refusal.BINDING_NOT_ACKED)
    _require(
        conn.execute(CAPTURES_QUERY, (publication.run_id,)).fetchall() == [], Refusal.CAPTURE_ALREADY_EXISTS
    )
    _require(
        conn.execute(CAPTURE_PER_RUN_QUERY, (publication.run_id,)).fetchall() == [],
        Refusal.CAPTURE_ALREADY_EXISTS,
    )
    _require(
        conn.execute(eq.RECEIPT_ID_QUERY, (publication.receipt_id,)).fetchall() == [],
        Refusal.RECEIPT_CONFLICT,
    )
    seq = _next_seq(conn)
    attempt.changed = True
    cursor = _write(
        conn,
        "INSERT INTO receipts(receipt_id,action,run_id,payload,acked) VALUES(?,'capture',?,?,0)",
        (publication.receipt_id, publication.run_id, payload),
    )
    if cursor.lastrowid != seq:
        raise eq._Failed(Fault.SNAPSHOT_FAILURE)
    choices = publication.choices
    _write(
        conn,
        "INSERT INTO captures(run_id,receipt_id,principal,cutoff_utc,policy,snapshot,assignment) "
        "VALUES(?,?,?,?,?,NULL,NULL)",
        (publication.run_id, publication.receipt_id, choices.principal, choices.cutoff_utc, policy),
    )
    after = _advance(conn, expected, seq)
    episode = CaptureEpisode(
        seq, "capture", publication.run_id, seq, publication.receipt_id, payload, choices, None, None
    )
    _after(conn, after, episode, publication.claim)
    _commit(conn, attempt, after, episode, None)


def _acknowledge_capture(
    conn: sqlite3.Connection,
    attempt: _Attempt,
    expected: ExpectedControl,
    claim: tuple[str, str, dict[str, Any]],
) -> None:
    receipt_id, text, snapshot = claim
    episode, bound = _episode(conn, expected)
    _require(episode.action == "capture", Refusal.WRONG_ACTION)
    _require(expected.acked_counter < MAX_INT and expected.revision < MAX_INT, Refusal.EXHAUSTED)
    _correlate(snapshot, episode, expected, receipt_id == episode.capture_receipt_id)
    assignment = _derive(snapshot, episode.run_id, episode.choices)
    attempt.changed = True
    _write(conn, "UPDATE receipts SET acked=1 WHERE seq=? AND acked=0", (episode.seq,))
    _write(
        conn,
        "UPDATE captures SET snapshot=?,assignment=? WHERE run_id=? AND receipt_id=? AND snapshot IS NULL",
        (text, assignment, episode.run_id, episode.capture_receipt_id),
    )
    after = _resolve_counter(conn, expected)
    _resolved(conn, after, episode, bound, parent_acked=True, snapshot=text, assignment=assignment, link=None)
    _commit(conn, attempt, after, None, CapturedCompletion(text, assignment))


def _publish_abort(
    conn: sqlite3.Connection,
    attempt: _Attempt,
    expected: ExpectedControl,
    publication: CaptureAbortPublication,
) -> None:
    episode, claim = _episode(conn, expected)
    _require(episode.run_id == publication.run_id, Refusal.PUBLICATION_CONFLICT)
    _require(episode.capture_receipt_id == publication.capture_receipt_id, Refusal.PUBLICATION_CONFLICT)
    if episode.action == "capture_abort":
        _require(episode.abort_receipt_id == publication.receipt_id, Refusal.PUBLICATION_CONFLICT)
        attempt.control, attempt.episode = expected, episode
        eq._rollback(conn, attempt)
        return
    _require(expected.revision < MAX_INT, Refusal.EXHAUSTED)
    _require(
        conn.execute(eq.RECEIPT_ID_QUERY, (publication.receipt_id,)).fetchall() == [],
        Refusal.RECEIPT_CONFLICT,
    )
    _require(
        conn.execute(LINK_BY_ABORT_QUERY, (publication.receipt_id,)).fetchall() == [],
        Refusal.RECEIPT_CONFLICT,
    )
    seq = _next_seq(conn)
    abort_text = _abort_request(episode.capture_payload, publication.receipt_id, episode.capture_receipt_id)
    _require(len(abort_text) <= REQUEST_MAX, Refusal.INVALID_INPUT)
    attempt.changed = True
    cursor = _write(
        conn,
        "INSERT INTO receipts(receipt_id,action,run_id,payload,acked) VALUES(?,'capture_abort',?,?,0)",
        (publication.receipt_id, publication.run_id, abort_text),
    )
    if cursor.lastrowid != seq:
        raise eq._Failed(Fault.SNAPSHOT_FAILURE)
    _write(
        conn,
        "INSERT INTO capture_aborts(parent_receipt_id,abort_receipt_id,outcome,proof) VALUES(?,?,NULL,NULL)",
        (episode.capture_receipt_id, publication.receipt_id),
    )
    after = _advance(conn, expected, seq)
    replaced = CaptureEpisode(
        seq,
        "capture_abort",
        episode.run_id,
        episode.capture_seq,
        episode.capture_receipt_id,
        episode.capture_payload,
        episode.choices,
        publication.receipt_id,
        abort_text,
    )
    _after(conn, after, replaced, claim)
    _commit(conn, attempt, after, replaced, None)


def _resolve_abort(
    conn: sqlite3.Connection,
    attempt: _Attempt,
    expected: ExpectedControl,
    claim: tuple[str, str, dict[str, Any], tuple[str, dict[str, Any]] | None],
) -> None:
    receipt_id, proof_text, proof, applied = claim
    episode, bound = _episode(conn, expected)
    _require(episode.action == "capture_abort", Refusal.WRONG_ACTION)
    _require(expected.acked_counter < MAX_INT and expected.revision < MAX_INT, Refusal.EXHAUSTED)
    q = expected.acked_counter
    _require(
        receipt_id == episode.abort_receipt_id
        and proof["journal_id"] == expected.journal_id
        and proof["run_id"] == episode.run_id
        and proof["abort_id"] == episode.abort_receipt_id
        and proof["capture_id"] == episode.capture_receipt_id
        and proof["capture_counter"] == q
        and proof["counter"] == q + 1,
        Refusal.ACK_REFUSED,
    )
    snapshot_text: str | None = None
    assignment: bytes | None = None
    if applied is not None:
        snapshot_text, snapshot = applied
        _correlate(snapshot, episode, expected, True)
        assignment = _derive(snapshot, episode.run_id, episode.choices)
    attempt.changed = True
    _write(conn, "UPDATE receipts SET acked=1 WHERE seq=? AND acked=0", (episode.seq,))
    if applied is not None:
        _write(
            conn,
            "UPDATE receipts SET acked=1 WHERE receipt_id=? AND action='capture' AND acked=0",
            (episode.capture_receipt_id,),
        )
        _write(
            conn,
            "UPDATE captures SET snapshot=?,assignment=? WHERE run_id=? AND receipt_id=? AND snapshot IS NULL",
            (snapshot_text, assignment, episode.run_id, episode.capture_receipt_id),
        )
    outcome = "aborted" if applied is None else "applied"
    _write(
        conn,
        "UPDATE capture_aborts SET outcome=?,proof=? WHERE abort_receipt_id=? AND parent_receipt_id=? "
        "AND outcome IS NULL",
        (outcome, proof_text, episode.abort_receipt_id, episode.capture_receipt_id),
    )
    after = _resolve_counter(conn, expected)
    _resolved(
        conn,
        after,
        episode,
        bound,
        parent_acked=applied is not None,
        snapshot=snapshot_text,
        assignment=assignment,
        link=(outcome, proof_text),
    )
    completion: Completion = (
        AbortedCompletion(proof_text)
        if snapshot_text is None
        else AppliedAfterAbortCompletion(snapshot_text, proof_text)
    )
    _commit(conn, attempt, after, None, completion)


def _correlate(
    snapshot: dict[str, Any], episode: CaptureEpisode, expected: ExpectedControl, request: bool
) -> None:
    _require(
        request
        and snapshot["journal_id"] == expected.journal_id
        and snapshot["counter"] == expected.acked_counter + 1
        and snapshot["capture_id"] == episode.capture_receipt_id
        and snapshot["run_id"] == episode.run_id
        and snapshot["principal"] == episode.choices.principal,
        Refusal.ACK_REFUSED,
    )


def _resolve_counter(conn: sqlite3.Connection, expected: ExpectedControl) -> ExpectedControl:
    _write(
        conn,
        "UPDATE identity SET acked_counter=? WHERE singleton=1 AND acked_counter=?",
        (expected.acked_counter + 1, expected.acked_counter),
    )
    _write(
        conn,
        "UPDATE effective_queue SET revision=?,active_seq=NULL WHERE singleton=1 AND revision=? AND active_seq=?",
        (expected.revision + 1, expected.revision, expected.active_seq),
    )
    return ExpectedControl(
        expected.journal_id, expected.generation, expected.revision + 1, None, expected.acked_counter + 1
    )


def _resolved(
    conn: sqlite3.Connection,
    after: ExpectedControl,
    episode: CaptureEpisode,
    bound: BindingClaim,
    *,
    parent_acked: bool,
    snapshot: str | None,
    assignment: bytes | None,
    link: tuple[str, str] | None,
) -> None:
    try:
        if eq._read_control(conn) != after:
            raise eq._Failed(Fault.SNAPSHOT_FAILURE)
        selected = _receipt(conn.execute(eq.SELECTED_QUERY, (episode.seq,)).fetchall())
        _require(selected["acked"] == 1 and selected["run_id"] == episode.run_id)
        if episode.action == "capture_abort":
            _require(
                (selected["receipt_id"], selected["action"], selected["payload"])
                == (episode.abort_receipt_id, "capture_abort", episode.abort_payload)
            )
            parent = _receipt(conn.execute(RECEIPT_BY_ID_QUERY, (episode.capture_receipt_id,)).fetchall())
            links = conn.execute(LINK_BY_ABORT_QUERY, (episode.abort_receipt_id,)).fetchall()
            _require(len(links) == 1)
            row = _row(links[0], _LINK, frozenset(("outcome", "proof")))
            _require(
                (row["parent_receipt_id"], row["outcome"], row["proof"])
                == (episode.capture_receipt_id, *(link or (None, None)))
            )
        else:
            parent = selected
        _require(
            (
                parent["seq"],
                parent["receipt_id"],
                parent["action"],
                parent["run_id"],
                parent["payload"],
                parent["acked"],
            )
            == (
                episode.capture_seq,
                episode.capture_receipt_id,
                "capture",
                episode.run_id,
                episode.capture_payload,
                int(parent_acked),
            )
        )
        choices, context = _context(conn, episode.run_id, episode.capture_receipt_id, resolved=True)
        _require(choices == episode.choices)
        _require((context["snapshot"], context["assignment"]) == (snapshot, assignment))
        stored = eq._binding(conn, episode.run_id)
        _require(stored == (bound, True))
    except (eq._Refused, _Refused):
        raise eq._Failed(Fault.SNAPSHOT_FAILURE) from None


def _next_seq(conn: sqlite3.Connection) -> int:
    try:
        return eq._next_seq(conn)
    except eq._Refused as error:
        raise _Refused(Refusal(error.reason.value)) from None


def _commit(
    conn: sqlite3.Connection,
    attempt: _Attempt,
    after: ExpectedControl,
    episode: CaptureEpisode | None,
    completion: Completion | None,
) -> None:
    attempt.control, attempt.episode, attempt.completion = after, episode, completion
    attempt.commit_attempted = True
    conn.execute("COMMIT")
    attempt.mutation = MutationOutcome.COMMITTED


def _inputs(operation: str, argument: object) -> object:
    if operation == "publish_capture":
        return _publication(argument)
    if operation == "publish_abort":
        return _abort_publication(argument)
    if operation == "acknowledge_capture":
        receipt_id, packet = _packet(argument, CaptureAckClaim)
        text, snapshot = _snapshot_packet(packet)
        return receipt_id, text, snapshot
    if operation == "resolve_abort":
        receipt_id, packet = _packet(argument, AbortResolutionClaim)
        return (receipt_id, *_proof_packet(packet))
    return None


def _run(path: object, expected: object, operation: str, argument: object) -> CaptureResult:
    attempt = _Attempt()
    conn: sqlite3.Connection | None = None
    try:
        try:
            checked_path, control = eq._path(path), eq._control(expected)
            prepared = _inputs(operation, argument)
        except AttributeError:
            raise _Refused(Refusal.INVALID_INPUT) from None
        identity = eq._existing(checked_path, attempt)
        conn = sqlite3.connect(f"{checked_path.as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=0)
        conn.row_factory = sqlite3.Row
        check_private_file(checked_path)
        metadata = checked_path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity:
            raise StateError("ledger_replaced")
        for statement in ("PRAGMA busy_timeout=0", "PRAGMA trusted_schema=OFF", "PRAGMA synchronous=EXTRA"):
            conn.execute(statement)
        conn.execute("BEGIN IMMEDIATE")
        attempt.begun = True
        attempt.mutation = MutationOutcome.UNKNOWN
        eq._validate(conn)
        eq._check_control(conn, control)
        if operation == "publish_capture":
            assert isinstance(prepared, CapturePublication)
            _publish_capture(conn, attempt, control, prepared)
        elif operation == "publish_abort":
            assert isinstance(prepared, CaptureAbortPublication)
            _publish_abort(conn, attempt, control, prepared)
        elif operation == "acknowledge_capture":
            _acknowledge_capture(conn, attempt, control, prepared)  # type: ignore[arg-type]
        elif operation == "resolve_abort":
            _resolve_abort(conn, attempt, control, prepared)  # type: ignore[arg-type]
        else:
            attempt.control = control
            attempt.episode = None if control.active_seq is None else _episode(conn, control)[0]
            eq._rollback(conn, attempt)
        attempt.finished = True
    except _Refused as error:
        attempt.capture_refusal = error.reason
    except eq._Refused as error:
        attempt.capture_refusal = Refusal(error.reason.value)
    except eq._Failed as error:
        attempt.failed(error.reason)
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if (
            not attempt.begun
            and type(code) is int
            and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        ):
            attempt.capture_refusal = Refusal.BUSY
        else:
            attempt.failed(Fault.SQLITE_FAILURE)
    except (StateError, OSError, ValueError, TypeError, OverflowError):
        attempt.failed(Fault.FILE_FAILURE)
    finally:
        if conn is not None:
            eq._finish(conn, attempt)
    return attempt.outcome()


def observe_capture_episode(path: Path, *, expected: ExpectedControl) -> CaptureResult:
    return _run(path, expected, "observe", None)


def publish_capture(
    path: Path, *, expected: ExpectedControl, publication: CapturePublication
) -> CaptureResult:
    return _run(path, expected, "publish_capture", publication)


def acknowledge_capture(path: Path, *, expected: ExpectedControl, claim: CaptureAckClaim) -> CaptureResult:
    return _run(path, expected, "acknowledge_capture", claim)


def publish_capture_abort(
    path: Path, *, expected: ExpectedControl, publication: CaptureAbortPublication
) -> CaptureResult:
    return _run(path, expected, "publish_abort", publication)


def resolve_capture_abort(
    path: Path, *, expected: ExpectedControl, claim: AbortResolutionClaim
) -> CaptureResult:
    return _run(path, expected, "resolve_abort", claim)
