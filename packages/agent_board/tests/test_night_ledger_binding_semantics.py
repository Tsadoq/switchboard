import sqlite3
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import ledger_binding as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import CONNECT, JOURNAL, Tape
    from packages.agent_board.tests.test_night_ledger_binding import CLAIM, ledger_path, put, read
else:
    from test_night_ledger_arm import CONNECT, JOURNAL, Tape
    from test_night_ledger_binding import CLAIM, ledger_path, put, read

__all__ = ["ledger_path"]


def change(path: Path, sql: str, values: Any = ()) -> None:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(sql, values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "F0000000-0000-4000-8000-000000000000"),
        ("run_id", "bad"),
        ("boot_id", "F0000000-0000-4000-8000-000000000000"),
        ("boot_id", b"x" * 36),
        ("uid", -1),
        ("uid", False),
        ("uid", "1000"),
        ("uid", 2**63),
        ("invocation_id", "a" * 31),
        ("invocation_id", "a" * 33),
        ("invocation_id", "g" * 32),
        ("invocation_id", b"a" * 32),
        ("invocation_id", "0" * 32),
        ("cgroup", "x" * 4097),
        ("cgroup", CLAIM.cgroup + "/"),
        ("cgroup", CLAIM.cgroup + "/.."),
        ("cgroup", b"/wrong"),
        ("cgroup", CLAIM.cgroup + "\x00"),
    ],
)
def test_strict_claim_fields_before_even_live_sidecar_file_work(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    ledger_path.with_name(ledger_path.name + "-journal").write_bytes(b"retained")
    tape = Tape(monkeypatch)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=replace(CLAIM, **{field: value}))
    assert result.refusal == writer.Refusal.INVALID_INPUT and result.fault is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.stored is None
    assert not tape.connections and not tape.attempts


@pytest.mark.parametrize("value", [None, True, 123, b"x" * 36, "bad", "F0000000-0000-4000-8000-000000000000"])
def test_strict_journal_argument_before_missing_file(ledger_path: Path, value: Any) -> None:
    missing = ledger_path.with_name("not-created.sqlite")
    result = writer.publish_binding(missing, journal_id=value, claim=CLAIM)
    assert result.refusal == writer.Refusal.INVALID_INPUT and result.fault is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and not missing.exists()


def test_claim_subclass_uninitialized_instance_and_frozen_tampering_revalidated(ledger_path: Path) -> None:
    class Child(writer.BindingClaim):
        pass

    altered = replace(CLAIM)
    object.__setattr__(altered, "uid", True)
    invalids: list[Any] = [
        Child(*astuple(CLAIM)),
        object.__new__(writer.BindingClaim),
        astuple(CLAIM),
        {"run_id": CLAIM.run_id},
        altered,
    ]
    for invalid in invalids:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=invalid)
        assert result.refusal == writer.Refusal.INVALID_INPUT and result.fault is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED


@pytest.mark.parametrize("uid", [1, 2**63 - 1])
def test_uid_storage_boundaries_are_metadata_not_linux_account_or_root_exec_identity(
    ledger_path: Path,
    uid: int,
) -> None:
    claim = replace(CLAIM, uid=uid)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=claim)
    assert result.mutation == MutationOutcome.COMMITTED and result.stored == writer.StoredBinding(
        JOURNAL, claim, False
    )
    assert read(ledger_path) == [(*astuple(claim), 0)]


@pytest.mark.parametrize("field,value", [("uid", 1001), ("boot_id", JOURNAL), ("invocation_id", "b" * 32)])
@pytest.mark.parametrize("ack", [0, 1])
def test_each_changeable_claim_field_conflicts_without_rebinding_or_unacknowledging(
    ledger_path: Path,
    field: str,
    value: Any,
    ack: int,
) -> None:
    put(ledger_path, ack=ack)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=replace(CLAIM, **{field: value}))
    assert result.refusal == writer.Refusal.BINDING_CONFLICT and result.fault is None
    assert (
        result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False and result.stored is None
    )
    assert read(ledger_path) == [(*astuple(CLAIM), ack)]


@pytest.mark.parametrize("journal", [None, CLAIM.run_id])
def test_valid_wrong_identity_precedes_malformed_binding_and_never_reads_it(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: str | None,
) -> None:
    put(ledger_path)
    change(ledger_path, "UPDATE bindings SET uid=0")
    change(ledger_path, "UPDATE identity SET journal_id=?", (journal,))
    tape = Tape(monkeypatch)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
    assert result.refusal == writer.Refusal.LEDGER_IDENTITY_MISMATCH and result.fault is None
    assert writer.BINDING_QUERY not in tape.attempts and len(tape.attempts) == 9
    tape.reconcile()


@pytest.mark.parametrize(
    "sql,values",
    [
        ("DELETE FROM identity", ()),
        ("UPDATE identity SET singleton=2", ()),
        ("INSERT INTO identity VALUES(2,?,0)", (JOURNAL,)),
        ("UPDATE identity SET journal_id=?", ("bad",)),
        ("UPDATE identity SET journal_id=?", (b"a" * 36,)),
        ("UPDATE identity SET journal_id=?", ("x" * 1000000,)),
        ("UPDATE identity SET acked_counter=-1", ()),
        ("UPDATE identity SET acked_counter='bad'", ()),
        ("UPDATE identity SET acked_counter=1.5", ()),
        ("UPDATE identity SET acked_counter=1e30", ()),
    ],
)
def test_bad_identity_refuses_before_mismatch_or_binding_work(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
    values: Any,
) -> None:
    change(ledger_path, sql, values)
    tape = Tape(monkeypatch)
    result = writer.publish_binding(ledger_path, journal_id=CLAIM.run_id, claim=CLAIM)
    assert result.refusal == writer.Refusal.ROW_REFUSED and result.fault is None
    assert result.stored is None and writer.BINDING_QUERY not in tape.attempts and len(tape.attempts) == 9
    tape.reconcile()


@pytest.mark.parametrize(
    "field,value",
    [
        ("uid", "not-a-number"),
        ("uid", 1.5),
        ("uid", 1e30),
        ("boot_id", "bad"),
        ("boot_id", "F0000000-0000-4000-8000-000000000000"),
        ("boot_id", b"a" * 36),
        ("boot_id", "x" * 1000000),
        ("invocation_id", "A" * 32),
        ("invocation_id", "g" * 32),
        ("invocation_id", b"a" * 32),
        ("invocation_id", "x" * 1000000),
        ("cgroup", b"/blob"),
        ("cgroup", CLAIM.cgroup + "/"),
        ("cgroup", "x" * 1000000),
        ("server_acked", -1),
        ("server_acked", 2),
        ("server_acked", "bad"),
        ("server_acked", 0.5),
    ],
)
def test_persisted_binding_kinds_caps_and_values_are_row_refusal_before_comparison(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    put(ledger_path, ack=1)
    change(ledger_path, f"UPDATE bindings SET {field}=?", (value,))
    before = read(ledger_path)
    tape = Tape(monkeypatch)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=replace(CLAIM, uid=1001))
    assert result.refusal == writer.Refusal.ROW_REFUSED and result.fault is None
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.stored is None
    assert len(tape.attempts) == len(tape.native) == 10
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in tape.attempts)
    assert read(ledger_path) == before
    tape.reconcile()


@pytest.mark.parametrize("field", ["boot_id", "invocation_id", "cgroup"])
def test_invalid_utf8_persisted_as_sqlite_text_is_refused_without_decoding_leak(
    ledger_path: Path, field: str
) -> None:
    put(ledger_path)
    change(ledger_path, f"UPDATE bindings SET {field}=CAST(x'fffe' AS TEXT)")
    before = ledger_path.read_bytes()
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
    assert result.refusal == writer.Refusal.ROW_REFUSED and result.fault is None
    assert result.mutation == MutationOutcome.ROLLED_BACK and ledger_path.read_bytes() == before


@pytest.mark.parametrize("field,cap", [("boot_id", 36), ("invocation_id", 32), ("cgroup", 4096)])
def test_real_projection_returns_cap_plus_one_not_a_truncated_valid_claim(
    ledger_path: Path,
    field: str,
    cap: int,
) -> None:
    put(ledger_path)
    change(ledger_path, f"UPDATE bindings SET {field}=?", ("x" * 1000000,))
    with closing(CONNECT(ledger_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(writer.BINDING_QUERY, (CLAIM.run_id,)).fetchone()
        assert type(row[field]) is bytes and len(row[field]) == cap + 1
        assert row[field + "_kind"] == "text"
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
    assert result.refusal == writer.Refusal.ROW_REFUSED and result.stored is None


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA user_version=0",
        "PRAGMA user_version=2",
        "PRAGMA user_version=4",
        "CREATE TABLE unexpected(value TEXT)",
        "CREATE TRIGGER unexpected AFTER INSERT ON bindings BEGIN SELECT 1; END",
        "CREATE INDEX unexpected ON bindings(uid)",
        "DROP TABLE bindings",
        "ALTER TABLE identity ADD COLUMN unexpected TEXT",
    ],
)
def test_schema_failure_precedes_identity_and_target_reads_without_migration(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
) -> None:
    change(ledger_path, sql)
    tape = Tape(monkeypatch)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
    assert result.refusal == writer.Refusal.SCHEMA_REFUSED and result.fault is None
    assert writer.IDENTITY_QUERY not in tape.attempts and writer.BINDING_QUERY not in tape.attempts
    assert result.mutation == MutationOutcome.ROLLED_BACK and len(tape.attempts) <= 8
    tape.reconcile()


@pytest.mark.parametrize("kind", ["name", "sql", "ddl"])
def test_sixteen_object_catalog_is_not_enough_and_actual_catalog_bytes_are_capped(
    ledger_path: Path,
    kind: str,
) -> None:
    with closing(CONNECT(ledger_path, isolation_level=None)) as conn:
        old = conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        conn.execute(f"DROP INDEX {old}")
        name = "x" * 1000 if kind == "name" else old
        predicate = "boot_id!='" + "x" * 10000 + "'" if kind == "sql" else "1=1"
        conn.execute(f"CREATE INDEX {name} ON bindings(boot_id) WHERE {predicate}")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(writer.CATALOG_QUERY).fetchall()
        assert len(rows) == 16
        if kind == "name":
            assert any(len(row["name"]) == 129 for row in rows)
        if kind == "sql":
            assert any(row["sql"] is not None and len(row["sql"]) == 8193 for row in rows)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
    assert result.refusal == writer.Refusal.SCHEMA_REFUSED and result.fault is None


def test_exact_scalar_types_and_nulls_do_not_gain_authority_from_dataclass_annotations(
    ledger_path: Path,
) -> None:
    class Text(str):
        pass

    class Number(int):
        pass

    def invalid(field: str, value: Any) -> writer.BindingClaim:
        return replace(CLAIM, **{field: value})

    claims = [invalid(field, None) for field in ("run_id", "uid", "boot_id", "invocation_id", "cgroup")]
    claims += [
        invalid(field, Text(getattr(CLAIM, field)))
        for field in ("run_id", "boot_id", "invocation_id", "cgroup")
    ]
    claims += [replace(CLAIM, uid=Number(1000)), replace(CLAIM, cgroup="x" * 1000000)]
    for claim in claims:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=claim)
        assert result.refusal == writer.Refusal.INVALID_INPUT and result.fault is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.stored is None
    result = writer.publish_binding(ledger_path, journal_id=Text(JOURNAL), claim=CLAIM)
    assert result.refusal == writer.Refusal.INVALID_INPUT and result.fault is None
    assert read(ledger_path) == []


def test_match_does_not_claim_current_call_created_preexisting_binding(ledger_path: Path) -> None:
    put(ledger_path, ack=1)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
    assert result.mutation == MutationOutcome.ROLLED_BACK and result.changed is False
    assert result.stored == writer.StoredBinding(JOURNAL, CLAIM, True)
    assert result.cleanup == CleanupOutcome.CLOSED and result.fault is None and result.refusal is None
