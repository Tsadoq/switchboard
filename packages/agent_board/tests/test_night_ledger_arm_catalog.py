import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import ledger_arm as writer
from agent_board.night.journal_latch import MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path
else:
    from test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path

__all__ = ["ledger_path"]


@pytest.mark.parametrize("case", ["long_name", "long_sql", "different_sql"])
def test_catalog_same_object_count_caps_and_exact_ddl_not_only_object_count(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    with closing(CONNECT(ledger_path, isolation_level=None)) as conn:
        original = conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        conn.execute(f"DROP INDEX {original}")
        name = "x" * 1000 if case == "long_name" else original
        predicate = "guard_boot_id!='" + "x" * 10000 + "'" if case == "long_sql" else "1=1"
        conn.execute(f"CREATE INDEX {name} ON arms(guard_boot_id) WHERE {predicate}")
        assert conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0] == 16
        conn.row_factory = sqlite3.Row
        catalog = conn.execute(writer.CATALOG_QUERY).fetchall()
        if case == "long_name":
            assert any(len(row["name"]) == 129 for row in catalog)
        if case == "long_sql":
            assert any(row["sql"] is not None and len(row["sql"]) == 8193 for row in catalog)
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert result.refusal == writer.Refusal.SCHEMA_REFUSED and result.fault is None
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.stored is None
    assert writer.IDENTITY_QUERY not in tape.attempts and len(tape.attempts) == 8
    tape.reconcile()


@pytest.mark.parametrize("counter", [0, 2**63 - 1])
def test_control_counter_boundaries_preserved_and_identity_projection_caps(
    ledger_path: Path,
    counter: int,
) -> None:
    with closing(CONNECT(ledger_path, isolation_level=None)) as conn:
        conn.execute("UPDATE identity SET acked_counter=?", (counter,))
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert result.mutation == MutationOutcome.COMMITTED and result.stored is not None
        assert conn.execute("SELECT acked_counter FROM identity").fetchone()[0] == counter
        conn.execute("UPDATE identity SET journal_id=?", ("x" * 1000000,))
        conn.row_factory = sqlite3.Row
        projected = conn.execute(writer.IDENTITY_QUERY).fetchone()
        assert projected["journal_id_kind"] == "text" and len(projected["journal_id"]) == 37
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert result.refusal == writer.Refusal.ROW_REFUSED and result.fault is None


def test_uri_reserved_filename_is_existing_file_not_selector_injection(ledger_path: Path) -> None:
    renamed = ledger_path.with_name("ledger ?#% ' Unicode-\u00e8.sqlite")
    ledger_path.rename(renamed)
    result = writer.compare_and_set_arm(renamed, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert result.mutation == MutationOutcome.COMMITTED and result.stored is not None
    assert not ledger_path.exists()
