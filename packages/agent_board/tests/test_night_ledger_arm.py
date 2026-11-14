import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from agent_board.night import ledger_arm as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.ledger import Arm, Ledger

CONNECT = sqlite3.connect
JOURNAL = "10000000-0000-4000-8000-000000000000"
ARM = Arm(
    "20000000-0000-4000-8000-000000000000", "30000000-0000-4000-8000-000000000000", "a" * 32, 1000.0, 2000.0
)


@pytest.fixture
def ledger_path() -> Iterator[Path]:
    with TemporaryDirectory(prefix="la.", dir=Path.home()) as directory:
        path = Path(directory) / "ledger.sqlite"
        Ledger(path)
        with closing(CONNECT(path, isolation_level=None)) as conn:
            conn.execute("UPDATE identity SET journal_id=?", (JOURNAL,))
        yield path


def put(path: Path, arm: Arm) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("INSERT INTO arms VALUES(?,?,?,?,?)", astuple(arm))


def read(path: Path) -> list[tuple[Any, ...]]:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        return conn.execute("SELECT * FROM arms ORDER BY run_id").fetchall()


class Tape:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.attempts: list[str] = []
        self.native: list[str] = []
        self.closes = 0
        tape = self

        class Connection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                tape.attempts.append(sql)
                return super().execute(sql, parameters)

            def close(self) -> None:
                tape.closes += 1
                super().close()

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            assert kwargs == {"uri": True, "isolation_level": None, "timeout": 0}
            assert args[0].endswith("?mode=rw")
            conn = CONNECT(*args, **kwargs, factory=Connection)
            conn.set_trace_callback(self.native.append)
            return conn

        monkeypatch.setattr(sqlite3, "connect", connect)


@pytest.mark.parametrize(
    "branch,count", [("insert", 12), ("replace", 12), ("noop", 10), ("conflict", 10), ("identity", 9)]
)
def test_actual_api_native_statement_budgets_and_transition_results(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    count: int,
) -> None:
    if branch in ("replace", "noop", "conflict"):
        put(ledger_path, ARM)
    desired = replace(ARM, guard_invocation_id="b" * 32) if branch == "replace" else ARM
    expected = ARM if branch in ("replace", "noop") else None
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(
        ledger_path,
        journal_id=JOURNAL if branch != "identity" else ARM.run_id,
        desired=desired,
        expected_prior=expected,
    )
    assert len(tape.native) == len(tape.attempts) == count
    assert tape.closes == 1 and result.cleanup == CleanupOutcome.CLOSED and result.fault is None
    if branch in ("conflict", "identity"):
        assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
        assert result.stored is None
        assert result.refusal == (
            writer.Refusal.ARM_CONFLICT if branch == "conflict" else writer.Refusal.LEDGER_IDENTITY_MISMATCH
        )
        assert tape.native[-1] == "ROLLBACK"
    else:
        assert result.refusal is None and result.stored == writer.StoredArm(JOURNAL, desired)
        assert result.mutation == (
            MutationOutcome.ROLLED_BACK if branch == "noop" else MutationOutcome.COMMITTED
        )
        assert result.changed is (branch != "noop")
        assert read(ledger_path) == [astuple(desired)]
        if branch == "noop":
            assert tape.native[-1] == "ROLLBACK" and "COMMIT" not in tape.native
        else:
            assert tape.native[-1] == "COMMIT"


def test_live_legacy_writer_journal_is_presence_refusal_not_a_damage_diagnosis(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = Ledger(ledger_path)
    with legacy._transaction() as conn:
        conn.execute("INSERT INTO arms VALUES(?,?,?,?,?)", astuple(ARM))
        journal = ledger_path.with_name(ledger_path.name + "-journal")
        assert journal.exists() and conn.in_transaction
        tape = Tape(monkeypatch)
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert result.refusal == writer.Refusal.SIDECAR_PRESENT
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
        assert result.fault is None and result.cleanup == CleanupOutcome.CLOSED and result.stored is None
        assert not tape.native and not tape.attempts and tape.closes == 0
        assert journal.exists() and conn.in_transaction
    assert read(ledger_path) == [astuple(ARM)]


def test_real_reserved_legacy_writer_without_journal_yields_typed_busy_before_begin(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = Ledger(ledger_path)
    with legacy._transaction() as conn:
        assert conn.in_transaction and not ledger_path.with_name(ledger_path.name + "-journal").exists()
        tape = Tape(monkeypatch)
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert result.refusal == writer.Refusal.BUSY and result.fault is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
        assert result.cleanup == CleanupOutcome.CLOSED and result.stored is None
        assert tape.attempts[-1] == "BEGIN IMMEDIATE" and tape.closes == 1
        assert not any(sql.startswith(("INSERT", "UPDATE", "ROLLBACK", "COMMIT")) for sql in tape.native)
    assert read(ledger_path) == []


def test_real_reader_blocks_commit_but_not_begin_and_keeps_unknown_after_rollback(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(CONNECT(ledger_path, isolation_level=None, timeout=0)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM identity").fetchall()
        tape = Tape(monkeypatch)
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert result.mutation == MutationOutcome.UNKNOWN and result.changed is None
        assert result.refusal is None and result.fault == writer.Fault.SQLITE_FAILURE
        assert result.cleanup == CleanupOutcome.CLOSED and result.stored is None
        assert len(tape.attempts) == len(tape.native) == 13
        assert tape.native[-2:] == ["COMMIT", "ROLLBACK"] and tape.closes == 1
        reader.execute("ROLLBACK")
    assert read(ledger_path) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "bad"),
        ("guard_invocation_id", "A" * 32),
        ("deadline_utc", 1),
        ("deadline_utc", True),
        ("deadline_utc", float("inf")),
        ("deadline_utc", -1.0),
    ],
)
def test_invalid_inputs_refuse_before_a_live_sidecar_or_sqlite(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    ledger_path.with_name(ledger_path.name + "-journal").write_bytes(b"retained")
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(
        ledger_path,
        journal_id=JOURNAL,
        desired=replace(ARM, **{field: value}),
        expected_prior=None,
    )
    assert result.refusal == writer.Refusal.INVALID_INPUT
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.fault is None
    assert not tape.attempts and not tape.native


def test_cross_run_expected_prior_is_invalid_even_when_ledger_is_missing(ledger_path: Path) -> None:
    missing = ledger_path.with_name("absent.sqlite")
    result = writer.compare_and_set_arm(
        missing,
        journal_id=JOURNAL,
        desired=ARM,
        expected_prior=replace(ARM, run_id=JOURNAL),
    )
    assert result.refusal == writer.Refusal.INVALID_INPUT and not missing.exists()
