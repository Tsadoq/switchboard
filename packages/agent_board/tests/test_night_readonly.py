import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night import readonly
from agent_board.night.assignment import NightPolicy
from agent_board.night.ledger import CaptureContext, Ledger, Pending
from agent_board.night.readonly import ReadOnlyError, ReadOnlyJournal, ReadOnlyLedger, bound_run
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import ClockReading, Grant, LocalJournal

BOOT = str(uuid4())
UID = 2001
FENCE = "f" * 43
INVOCATION = "b" * 32
JOURNAL_ID = str(uuid4())
POLICY = NightPolicy("sb-runner", "a" * 40, "claude", None, "default", 600)


@pytest.fixture(autouse=True)
def protected_fixture_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def metadata(path: Path) -> os.stat_result:
        result = path.lstat()
        if path in tmp_path.parents:
            values = list(result)
            values[0] = 0o40755
            values[4] = 0
            return os.stat_result(values)
        return result

    monkeypatch.setattr(night_state, "_ancestor_stat", metadata, raising=False)


class State:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = ClockReading(BOOT, 100.0, 1_000.0)
        self.journal_path = tmp_path / "state" / "journal.sqlite3"
        self.ledger_path = tmp_path / "state" / "ledger.sqlite3"
        self.journal = LocalJournal(
            self.journal_path, box="dev", principals={"pi@dev": UID}, clock=lambda: self.clock
        )
        self.ledger = Ledger(self.ledger_path)
        self.run_id = str(uuid4())
        self.cgroup = unit_cgroup(f"switchboard-night-{self.run_id}.service")
        self.journal.reserve(Grant(self.run_id, FENCE, "pi@dev", UID, BOOT, 900.0, 1_900.0))
        self.journal.claim_start(self.run_id, FENCE)
        self.ledger.record_binding(
            self.run_id, uid=UID, boot_id=BOOT, invocation_id=INVOCATION, cgroup=self.cgroup
        )
        binding = Pending(str(uuid4()), "binding", self.run_id, {"counter": 0})
        self.ledger.persist(binding)
        self.ledger.acknowledge(binding.receipt_id, journal_id=JOURNAL_ID, counter=1)
        self.capture_receipt = Pending(str(uuid4()), "capture", self.run_id, {"counter": 1})
        self.ledger.persist_capture(self.capture_receipt, CaptureContext("pi@dev", 1_850.0, POLICY))

    def acknowledge_capture(self, assignment: bytes | None = b"{}") -> None:
        self.ledger.acknowledge(
            self.capture_receipt.receipt_id,
            journal_id=JOURNAL_ID,
            counter=2,
            capture=('{"wakes": []}', assignment),
        )

    def bound(self, **changes: Any) -> Any:
        observed = {
            "cgroup": self.cgroup,
            "invocation_id": INVOCATION,
            "boot_id": BOOT,
            "boottime": 150.0,
            "utc": 1_050.0,
            "grace": 5.0,
            **changes,
        }
        return bound_run(ReadOnlyJournal(self.journal_path), ReadOnlyLedger(self.ledger_path), **observed)


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State(tmp_path)


def test_the_bound_run_comes_only_from_root_state_and_the_observed_cgroup(state: State) -> None:
    state.acknowledge_capture()

    found = state.bound()

    assert found.record.run_id == state.run_id
    assert found.binding.invocation_id == INVOCATION
    assert found.capture.receipt_id == state.capture_receipt.receipt_id


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param({"cgroup": "/user.slice/x"}, "no_single_run_for_cgroup", id="other-cgroup"),
        pytest.param({"invocation_id": "c" * 32}, "binding_mismatch", id="other-invocation"),
        pytest.param({"boot_id": str(uuid4())}, "run_not_running", id="other-boot"),
        pytest.param({"boottime": 896.0}, "run_past_deadline", id="boottime-within-grace"),
        pytest.param({"utc": 1_895.0}, "run_past_deadline", id="utc-within-grace"),
        pytest.param({"utc": float("nan")}, "invalid_clock", id="nan-clock"),
    ],
)
def test_any_observation_that_disagrees_refuses(state: State, changes: dict[str, Any], reason: str) -> None:
    state.acknowledge_capture()

    with pytest.raises(ReadOnlyError, match=reason):
        state.bound(**changes)


def test_a_stop_latch_or_an_unusable_capture_refuses(state: State) -> None:
    with pytest.raises(ReadOnlyError, match="no_usable_capture"):
        state.bound()
    state.acknowledge_capture(assignment=None)
    with pytest.raises(ReadOnlyError, match="no_usable_capture"):
        state.bound()


def test_a_commit_by_the_writer_is_visible_to_the_next_read(state: State) -> None:
    state.acknowledge_capture()
    view = ReadOnlyJournal(state.journal_path)
    assert not view.runs()[0].stop_requested

    state.journal.request_stop(state.run_id, FENCE)

    assert view.runs()[0].stop_requested
    with pytest.raises(ReadOnlyError, match="run_not_running"):
        state.bound()


def _digest(path: Path) -> tuple[str, float]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime


def test_reading_writes_nothing_not_even_the_expiry_refresh(state: State) -> None:
    state.acknowledge_capture()
    before = (_digest(state.journal_path), _digest(state.ledger_path))

    with pytest.raises(ReadOnlyError, match="run_past_deadline"):
        state.bound(utc=5_000.0)
    record = ReadOnlyJournal(state.journal_path).runs()[0]
    ReadOnlyLedger(state.ledger_path).identity()

    assert record.state == "starting" and not record.stop_requested, (
        "the view refreshed expiry the way active() does"
    )
    assert (_digest(state.journal_path), _digest(state.ledger_path)) == before


def _tamper(path: Path, *statements: str) -> None:
    with sqlite3.connect(path) as conn:
        for statement in statements:
            conn.execute(statement)


@pytest.mark.parametrize(
    ("statements", "reason"),
    [
        pytest.param(("PRAGMA user_version = 3",), "unexpected_schema_version", id="other-version"),
        pytest.param(("CREATE TABLE extra (x)",), "unexpected_tables", id="extra-table"),
        pytest.param(("ALTER TABLE runs ADD COLUMN injected TEXT",), "unexpected_columns", id="extra-column"),
    ],
)
def test_a_journal_with_another_schema_refuses(
    state: State, statements: tuple[str, ...], reason: str
) -> None:
    _tamper(state.journal_path, *statements)

    with pytest.raises(ReadOnlyError, match=reason):
        ReadOnlyJournal(state.journal_path).runs()


def test_a_ledger_with_another_schema_refuses(state: State) -> None:
    _tamper(state.ledger_path, "ALTER TABLE bindings RENAME COLUMN cgroup TO control_group")

    with pytest.raises(ReadOnlyError, match="unexpected_columns"):
        ReadOnlyLedger(state.ledger_path).binding(state.run_id)


def test_a_corrupt_file_is_a_typed_refusal(state: State) -> None:
    state.journal_path.write_bytes(b"not a database" * 100)

    with pytest.raises(ReadOnlyError, match="state_unreadable"):
        ReadOnlyJournal(state.journal_path).runs()


def test_a_corrupt_row_is_a_typed_refusal(state: State) -> None:
    _tamper(state.journal_path, "UPDATE runs SET uid = -1")

    with pytest.raises(ReadOnlyError, match="journal_row_corrupt"):
        ReadOnlyJournal(state.journal_path).runs()


def test_a_state_file_others_can_read_refuses_before_opening(state: State) -> None:
    state.journal_path.chmod(0o644)

    with pytest.raises(night_state.StateError):
        ReadOnlyJournal(state.journal_path).runs()


def test_the_view_cannot_write(state: State) -> None:
    view = readonly._read_only(state.journal_path, readonly.JOURNAL_VERSION, readonly.JOURNAL_TABLES)
    with view as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE runs SET stop_requested = 1")


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "UPDATE receipts SET action = 'binding' WHERE action = 'capture'", id="receipt-of-another-action"
        ),
        pytest.param(
            "UPDATE receipts SET run_id = 'another' WHERE action = 'capture'", id="receipt-of-another-run"
        ),
    ],
)
def test_a_capture_joined_to_the_wrong_receipt_is_not_usable(state: State, statement: str) -> None:
    state.acknowledge_capture()
    _tamper(state.ledger_path, statement)

    with pytest.raises(ReadOnlyError, match="no_usable_capture"):
        state.bound()


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("UPDATE captures SET snapshot = '123'", id="snapshot-not-an-object"),
        pytest.param("UPDATE captures SET assignment = 'not-bytes'", id="assignment-stored-as-text"),
        pytest.param("UPDATE identity SET acked_counter = 0.5", id="fractional-counter"),
        pytest.param("UPDATE bindings SET uid = 'two thousand'", id="uid-stored-as-text"),
    ],
)
def test_a_persisted_value_of_the_wrong_type_is_a_typed_refusal(state: State, statement: str) -> None:
    state.acknowledge_capture()
    _tamper(state.ledger_path, statement)

    with pytest.raises(ReadOnlyError):
        state.bound()
        ReadOnlyLedger(state.ledger_path).identity()


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"utc": True}, id="boolean-clock"),
        pytest.param({"boottime": 10**1000}, id="overflowing-clock"),
        pytest.param({"grace": "5"}, id="string-grace"),
        pytest.param({"grace": -1.0}, id="negative-grace"),
        pytest.param({"boottime": -1.0}, id="negative-boottime"),
        pytest.param({"utc": -1.0}, id="negative-utc"),
    ],
)
def test_a_malformed_clock_is_a_typed_refusal(state: State, changes: dict[str, Any]) -> None:
    state.acknowledge_capture()

    with pytest.raises(ReadOnlyError, match="invalid_clock"):
        state.bound(**changes)
