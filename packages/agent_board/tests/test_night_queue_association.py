import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq
from agent_board.night import queue_arm as qa
from agent_board.night import queue_association as qs
from agent_board.night.journal_latch import CleanupOutcome
from agent_board.night.ledger_binding import BindingClaim
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import ClockReading, Grant, LocalJournal

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue import POLICY, WAKE
    from packages.agent_board.tests.test_night_effective_queue import CONNECT, create_fixture
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_capture_queue import POLICY, WAKE
    from test_night_effective_queue import CONNECT, create_fixture
    from test_night_ledger_arm import Tape

JOURNAL = "10000000-0000-4000-8000-000000000000"
GENERATION = "40000000-0000-4000-8000-000000000000"
RUN = "20000000-0000-4000-8000-000000000000"
BOOT = "30000000-0000-4000-8000-000000000000"
BINDING_RECEIPT = "50000000-0000-4000-8000-000000000000"
CAPTURE = "80000000-0000-4000-8000-000000000000"
FENCE = "f" * 43
UID = 1000
GRANT = Grant(RUN, FENCE, "pi@dev", UID, BOOT, 1000.0, 2000.0)
NOW = ClockReading(BOOT, 100.0, 1100.0)
GRACE = 5.0
GUARD = qa.ArmTuple(RUN, BOOT, "c" * 32, 1000.0, 2000.0)
CLAIM = BindingClaim(RUN, UID, BOOT, "a" * 32, unit_cgroup(f"switchboard-night-{RUN}.service"))
CHOICES = cq.CaptureChoices("pi@dev", 1900.0, POLICY)


@dataclass
class Live:
    journal: Path
    ledger: Path
    control: eq.ExpectedControl
    completion: cq.CapturedCompletion

    def read(self, **changes: Any) -> qs.AssociationResult:
        arguments: dict[str, Any] = {
            "expected": GRANT,
            "control": self.control,
            "completion": self.completion,
            "guard": GUARD,
            "now": NOW,
            "grace": GRACE,
        }
        arguments |= changes
        journal = arguments.pop("journal_path", self.journal)
        ledger = arguments.pop("ledger_path", self.ledger)
        return qs.read_capture_association(journal, ledger, **arguments)


def snapshot(**changes: Any) -> dict[str, Any]:
    value = {
        "journal_id": JOURNAL,
        "counter": 2,
        "capture_id": CAPTURE,
        "run_id": RUN,
        "principal": "pi@dev",
        "deadline": 1950,
        "wakes": [WAKE],
    }
    return value | changes


def build(directory: Path, *, wakes: list[dict[str, Any]] | None = None) -> Live:
    journal_path, ledger_path = directory / "journal.sqlite3", directory / "ledger.sqlite3"
    journal = LocalJournal(journal_path, box="dev", principals={"pi@dev": UID}, clock=lambda: NOW)
    journal.reserve(GRANT)
    assert journal.claim_start(RUN, FENCE)
    create_fixture(ledger_path)
    control = eq.ExpectedControl(JOURNAL, GENERATION, 0, None, 0)
    armed = qa.compare_and_set_queue_arm(
        ledger_path, expected=control, publication=qa.ArmPublication(GUARD, None)
    )
    assert armed.control is not None, armed
    binding = eq.BindingPublication(BINDING_RECEIPT, RUN, FENCE, CLAIM)
    published = eq.publish_ordinary(ledger_path, expected=armed.control, publication=binding)
    assert published.control is not None, published
    ack = eq.OrdinaryAckClaim(BINDING_RECEIPT, JOURNAL, 1, "running")
    bound = eq.acknowledge_ordinary(ledger_path, expected=published.control, ack=ack)
    assert bound.control is not None, bound
    capture = cq.CapturePublication(CAPTURE, RUN, FENCE, CLAIM, CHOICES)
    captured = cq.publish_capture(ledger_path, expected=bound.control, publication=capture)
    assert captured.control is not None, captured
    value = snapshot() if wakes is None else snapshot(wakes=wakes)
    acked = cq.acknowledge_capture(
        ledger_path,
        expected=captured.control,
        claim=cq.CaptureAckClaim(CAPTURE, cq._canonical(value).encode()),
    )
    assert acked.control is not None and type(acked.completion) is cq.CapturedCompletion, acked
    return Live(journal_path, ledger_path, acked.control, acked.completion)


@pytest.fixture
def live() -> Iterator[Live]:
    with TemporaryDirectory(prefix="qs.", dir=Path.home()) as directory:
        yield build(Path(directory))


def ok(result: qs.AssociationResult) -> qs.CaptureAssociation:
    assert result.refusal is None and result.fault is None and result.association is not None, result
    assert (result.journal_cleanup, result.ledger_cleanup) == (CleanupOutcome.CLOSED, CleanupOutcome.CLOSED)
    return result.association


def refused(result: qs.AssociationResult, reason: qs.Refusal) -> None:
    assert result.refusal == reason, result
    assert result.association is None and result.fault is None


def sql(path: Path, statement: str, *parameters: object) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute(statement, parameters)


def test_a_coherent_first_start_returns_its_association(live: Live) -> None:
    association = ok(live.read())
    assert association == qs.CaptureAssociation(
        RUN,
        CAPTURE,
        JOURNAL,
        "pi@dev",
        UID,
        BOOT,
        "a" * 32,
        CLAIM.cgroup,
        BOOT,
        "c" * 32,
        1000.0,
        2000.0,
        live.completion.assignment,
    )
    assert association.assignment is not None


def test_the_success_schedule_is_25_statements_across_both_files(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, statement: str, parameters: Any = (), /) -> Any:
            attempts.append(statement)
            return super().execute(statement, parameters)

    def connect(*args: Any, **kwargs: Any) -> Any:
        assert kwargs == {"uri": True, "isolation_level": None, "timeout": 0} and args[0].endswith("?mode=ro")
        return original(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    ok(live.read())
    assert len(attempts) == 25 and attempts.count("ROLLBACK") == 2 and attempts.count("BEGIN") == 2, attempts


def test_empty_wakes_give_an_association_with_no_assignment() -> None:
    with TemporaryDirectory(prefix="qs.", dir=Path.home()) as directory:
        live = build(Path(directory), wakes=[])
        assert ok(live.read()).assignment is None


def test_reading_changes_neither_file(live: Live) -> None:
    before = (live.journal.read_bytes(), live.ledger.read_bytes())
    ok(live.read())
    assert (live.journal.read_bytes(), live.ledger.read_bytes()) == before


def test_a_stale_control_refuses(live: Live) -> None:
    refused(
        live.read(control=replace(live.control, revision=live.control.revision + 1)),
        qs.Refusal.CONTROL_MISMATCH,
    )


def test_another_journal_identity_refuses(live: Live) -> None:
    other = replace(live.control, journal_id="10000000-0000-4000-8000-000000000001")
    refused(live.read(control=other), qs.Refusal.ASSOCIATION_MISMATCH)


def test_an_active_queue_refuses_before_any_association_lookup(live: Live) -> None:
    binding = eq.BindingPublication(
        "70000000-0000-4000-8000-000000000000",
        "a0000000-0000-4000-8000-000000000000",
        FENCE,
        replace(
            CLAIM,
            run_id="a0000000-0000-4000-8000-000000000000",
            cgroup=unit_cgroup("switchboard-night-a0000000-0000-4000-8000-000000000000.service"),
        ),
    )
    active = eq.publish_ordinary(live.ledger, expected=live.control, publication=binding)
    assert active.control is not None
    refused(live.read(control=active.control), qs.Refusal.QUEUE_NOT_EMPTY)


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        pytest.param(
            "UPDATE runs SET state='reserved', launch_claimed=0", qs.Refusal.JOURNAL_INELIGIBLE, id="reserved"
        ),
        pytest.param("UPDATE runs SET stop_requested=1", qs.Refusal.JOURNAL_INELIGIBLE, id="stop-requested"),
        pytest.param("UPDATE runs SET state='stopping'", qs.Refusal.JOURNAL_INELIGIBLE, id="stopping"),
        pytest.param("UPDATE runs SET uncertain=1", qs.Refusal.JOURNAL_INELIGIBLE, id="uncertain"),
        pytest.param("UPDATE runs SET reason='x'", qs.Refusal.JOURNAL_INELIGIBLE, id="reason"),
        pytest.param("UPDATE runs SET uid=1001", qs.Refusal.ASSOCIATION_MISMATCH, id="uid"),
        pytest.param("UPDATE runs SET deadline_utc=2001.0", qs.Refusal.ASSOCIATION_MISMATCH, id="deadline"),
        pytest.param("UPDATE runs SET reason=x'00'", qs.Refusal.ROW_REFUSED, id="reason-as-blob"),
        pytest.param("UPDATE runs SET uid='abc'", qs.Refusal.ROW_REFUSED, id="uid-as-text"),
        pytest.param("PRAGMA user_version=3", qs.Refusal.SCHEMA_REFUSED, id="journal-version"),
        pytest.param("CREATE TABLE extra(x)", qs.Refusal.SCHEMA_REFUSED, id="journal-extra-table"),
    ],
)
def test_journal_state_and_rows_refuse_with_their_reason(
    live: Live, statement: str, reason: qs.Refusal
) -> None:
    sql(live.journal, statement)
    refused(live.read(), reason)


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        pytest.param(
            "UPDATE bindings SET server_acked=0", qs.Refusal.ASSOCIATION_MISMATCH, id="binding-unacked"
        ),
        pytest.param("UPDATE bindings SET uid=0", qs.Refusal.ROW_REFUSED, id="binding-malformed"),
        pytest.param(
            "UPDATE captures SET snapshot=snapshot||' '",
            qs.Refusal.ASSOCIATION_MISMATCH,
            id="snapshot-differs",
        ),
        pytest.param(
            "UPDATE captures SET principal='claude@dev'",
            qs.Refusal.ASSOCIATION_MISMATCH,
            id="capture-principal",
        ),
        pytest.param(
            "UPDATE captures SET policy=replace(policy,'600','601')",
            qs.Refusal.ASSOCIATION_MISMATCH,
            id="policy-derivation",
        ),
        pytest.param(
            "UPDATE captures SET assignment=NULL", qs.Refusal.ASSOCIATION_MISMATCH, id="stored-assignment"
        ),
        pytest.param(
            "UPDATE receipts SET acked=0 WHERE action='capture'",
            qs.Refusal.ASSOCIATION_MISMATCH,
            id="receipt-unacked",
        ),
        pytest.param(
            f"INSERT INTO capture_aborts(parent_receipt_id,abort_receipt_id) VALUES('{CAPTURE}','90000000-0000-4000-8000-000000000000')",
            qs.Refusal.CAPTURE_ABORTED,
            id="abort-link",
        ),
        pytest.param(
            "UPDATE arms SET guard_invocation_id='d'||substr(guard_invocation_id,2)",
            qs.Refusal.ASSOCIATION_MISMATCH,
            id="arm-differs",
        ),
        pytest.param("DELETE FROM arms", qs.Refusal.ASSOCIATION_MISMATCH, id="arm-missing"),
    ],
)
def test_ledger_rows_refuse_with_their_reason(live: Live, statement: str, reason: qs.Refusal) -> None:
    sql(live.ledger, statement)
    refused(live.read(), reason)


def test_a_guard_from_another_boot_or_run_refuses_before_opening_files(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    tape = Tape(monkeypatch)
    for guard in (
        replace(GUARD, guard_boot_id="30000000-0000-4000-8000-000000000001"),
        replace(GUARD, deadline_utc=2001.0),
    ):
        refused(live.read(guard=guard), qs.Refusal.ASSOCIATION_MISMATCH)
    assert tape.attempts == []


def test_an_expired_grant_refuses(live: Live) -> None:
    refused(live.read(now=ClockReading(BOOT, 100.0, 1996.0)), qs.Refusal.EXPIRED)
    refused(live.read(now=ClockReading(BOOT, 996.0, 1100.0)), qs.Refusal.EXPIRED)


def test_an_assignment_deadline_inside_grace_refuses(live: Live) -> None:
    refused(live.read(now=ClockReading(BOOT, 100.0, 1946.0), grace=0.0), qs.Refusal.EXPIRED)


def test_an_unusable_completion_refuses_before_opening_files(live: Live) -> None:
    for text in ("{", "x" * 65537, cq._canonical(snapshot()).replace("1950", "1e999")):
        refused(live.read(completion=cq.CapturedCompletion(text, None)), qs.Refusal.COMPLETION_UNUSABLE)


def test_a_completion_that_does_not_match_refuses(live: Live) -> None:
    other = cq.CapturedCompletion(live.completion.snapshot, b"{}")
    refused(live.read(completion=other), qs.Refusal.ASSOCIATION_MISMATCH)


def test_aliased_or_unsafe_paths_refuse(live: Live) -> None:
    refused(live.read(ledger_path=live.journal), qs.Refusal.PATH_REFUSED)
    live.ledger.chmod(0o644)
    refused(live.read(), qs.Refusal.PATH_REFUSED)
    live.ledger.chmod(0o600)
    sidecar = live.ledger.with_name(live.ledger.name + "-journal")
    sidecar.write_bytes(b"x")
    refused(live.read(), qs.Refusal.SIDECAR_PRESENT)


def test_malformed_inputs_are_invalid_before_any_sql(live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    tape = Tape(monkeypatch)
    for change in (
        {"expected": object()},
        {"grace": 5},
        {"grace": float("nan")},
        {"now": object()},
        {"guard": object()},
        {"completion": cq.AbortedCompletion("{}")},
        {"journal_path": str(live.journal)},
    ):
        refused(live.read(**change), qs.Refusal.INVALID_INPUT)
    assert tape.attempts == []


def test_a_held_writer_lock_is_a_sqlite_failure_after_begin(live: Live) -> None:
    holder = sqlite3.connect(live.journal, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        result = live.read()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert (result.refusal, result.fault, result.association) == (None, qs.Fault.SQLITE_FAILURE, None), result
    assert (result.journal_cleanup, result.ledger_cleanup) == (CleanupOutcome.CLOSED, CleanupOutcome.CLOSED)
