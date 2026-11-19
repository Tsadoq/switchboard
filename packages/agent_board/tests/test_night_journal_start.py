import hashlib
import itertools
import os
import sqlite3
import subprocess
import sys
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from agent_board.night import journal_start as js
from agent_board.night import queue_association as qs
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.journal_start import Fault, Refusal, StartClaimResult, claim_start
from agent_board.night_state import ClockReading, Grant, LocalJournal, Record

BOOT = "3000000a-0000-4000-8000-00000000000b"
OTHER_BOOT = "30000000-0000-4000-8000-000000000001"
RUN = "2000000c-0000-4000-8000-00000000000d"
OTHER_RUN = "2000000c-0000-4000-8000-00000000000e"
FENCE = "F" * 43
PRINCIPAL = "pi@huginn"
UID = 1001
NOW = ClockReading(BOOT, 100.0, 1_000.0)
GRANT = Grant(RUN, FENCE, PRINCIPAL, UID, BOOT, 700.0, 1_600.0)
PRINCIPALS = {PRINCIPAL: UID}
GRACE = 5.0
N, C, RB, U = (
    MutationOutcome.NOT_ATTEMPTED,
    MutationOutcome.COMMITTED,
    MutationOutcome.ROLLED_BACK,
    MutationOutcome.UNKNOWN,
)
CLOSED, UNCERTAIN = CleanupOutcome.CLOSED, CleanupOutcome.UNCERTAIN
CONNECT = sqlite3.connect


def legacy(path: Path, clock: ClockReading = NOW) -> LocalJournal:
    return LocalJournal(path, box="huginn", principals=PRINCIPALS, clock=lambda: clock)


@pytest.fixture
def journal() -> Iterator[Path]:
    with TemporaryDirectory(prefix="js.", dir=Path.home()) as directory:
        path = Path(directory) / "state" / "journal.sqlite3"
        legacy(path)
        yield path


def claim(path: Path, **changes: Any) -> StartClaimResult:
    arguments: dict[str, Any] = {
        "expected": GRANT,
        "principals": dict(PRINCIPALS),
        "now": NOW,
        "grace": GRACE,
    }
    arguments.update(changes)
    return claim_start(path, **arguments)


def rows(path: Path) -> list[tuple[Any, ...]]:
    with closing(CONNECT(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT * FROM runs ORDER BY run_id").fetchall()


def result(
    mutation: MutationOutcome,
    cleanup: CleanupOutcome = CLOSED,
    refusal: Refusal | None = None,
    fault: Fault | None = None,
) -> StartClaimResult:
    return StartClaimResult(mutation, cleanup, refusal, fault)


def refused(reason: Refusal) -> StartClaimResult:
    return result(N, refusal=reason)


def sidecars(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name != path.name)


def test_constants_are_the_pinned_association_constants() -> None:
    assert vars(js)["RUN_QUERY"] is qs.RUN_QUERY
    assert vars(js)["ACTIVE_QUERY"] is qs.ACTIVE_QUERY
    assert vars(js)["JOURNAL_CATALOG_QUERY"] is qs.JOURNAL_CATALOG_QUERY
    assert vars(js)["_JOURNAL_CATALOG"] is qs._JOURNAL_CATALOG
    assert vars(js)["_RUN"] is qs._RUN
    assert vars(js)["LEGACY_FENCE"] is qs.FENCE
    assert (vars(js)["MAX_RUN_SECONDS"], vars(js)["MIN_START_SECONDS"]) == (1200, 6)
    assert js.SETUP == (
        "PRAGMA busy_timeout=0",
        "PRAGMA synchronous=EXTRA",
        "PRAGMA trusted_schema=OFF",
        "BEGIN IMMEDIATE",
    )


REVIEWED_DIGESTS = {
    "RUN_QUERY": "96dcb226ab00264211776d9891e67c538f6ccde11bb76985bba32521cef1f069",
    "JOURNAL_CATALOG_QUERY": "91b61a289674b09b44d41e2310136a29e7d489c58d54bcca9407c20c49fcb039",
    "_JOURNAL_CATALOG": "0f48619a5c6a9a1c75ed27a3fa74b3620fe8fc22bb10a28e01c7ade874c6406e",
}
REVIEWED_RUN = {
    "run_id": ("text", 36),
    "fence": ("text", 128),
    "principal": ("text", 128),
    "uid": ("integer", 0),
    "boot_id": ("text", 36),
    "deadline_boottime": ("real", 0),
    "deadline_utc": ("real", 0),
    "state": ("text", 16),
    "launch_claimed": ("integer", 0),
    "stop_requested": ("integer", 0),
    "uncertain": ("integer", 0),
    "reason": ("text", 256),
    "slot": ("integer", 0),
}


def _digest(name: str, value: object) -> str:
    text = repr(sorted(value.items())) if isinstance(value, dict) else str(value)
    return hashlib.sha256(text.encode()).hexdigest()


def test_reused_constants_match_reviewed_content() -> None:
    for name, digest in REVIEWED_DIGESTS.items():
        assert _digest(name, vars(js)[name]) == digest
    assert list(vars(js)["_RUN"].items()) == list(REVIEWED_RUN.items())
    assert vars(js)["ACTIVE_QUERY"] == (
        "SELECT substr(CAST(run_id AS BLOB),1,37) AS run_id,typeof(run_id) AS run_id_kind "
        "FROM runs INDEXED BY one_active_run WHERE slot=1 AND state != 'stopped' LIMIT 2"
    )
    assert vars(js)["JOURNAL_VERSION"] == 2
    assert vars(js)["LEGACY_FENCE"].pattern == "[A-Za-z0-9_-]{32,128}"
    assert js.FENCE.pattern == "[A-Za-z0-9_-]{43}"
    assert vars(js)["RUN_QUERY"].endswith(
        "FROM runs INDEXED BY sqlite_autoindex_runs_1 WHERE run_id=? LIMIT 2"
    )
    assert vars(js)["JOURNAL_CATALOG_QUERY"].endswith("FROM sqlite_schema LIMIT 4")
    assert js.INSERT == (
        "INSERT INTO runs (run_id, fence, principal, uid, boot_id, deadline_boottime, deadline_utc, "
        "state, launch_claimed, stop_requested, uncertain, reason, slot) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'starting', 1, 0, 0, NULL, 1)"
    )


def test_reviewed_digest_detects_a_widened_limit() -> None:
    widened = qs.JOURNAL_CATALOG_QUERY.replace("LIMIT 4", "LIMIT 4000")
    assert widened != qs.JOURNAL_CATALOG_QUERY
    assert _digest("JOURNAL_CATALOG_QUERY", widened) != REVIEWED_DIGESTS["JOURNAL_CATALOG_QUERY"]


def test_claim_inserts_one_starting_claimed_row(journal: Path) -> None:
    outcome = claim(journal)
    assert outcome == result(C)
    assert outcome.usable
    assert rows(journal) == [(RUN, FENCE, PRINCIPAL, UID, BOOT, 700.0, 1600.0, "starting", 1, 0, 0, None, 1)]
    assert journal.lstat().st_mode & 0o777 == 0o600
    assert sidecars(journal) == []


def test_repeated_claim_refuses_without_change(journal: Path) -> None:
    assert claim(journal).usable
    before = journal.read_bytes()
    assert claim(journal) == refused(Refusal.RUN_ALREADY_RECORDED)
    assert journal.read_bytes() == before


def test_another_run_refuses_while_one_is_active(journal: Path) -> None:
    assert claim(journal).usable
    other = replace(GRANT, run_id=OTHER_RUN)
    assert claim(journal, expected=other) == refused(Refusal.ACTIVE_PRESENT)
    assert [row[0] for row in rows(journal)] == [RUN]


def test_expired_active_row_is_neither_refreshed_nor_released(journal: Path) -> None:
    legacy(journal).reserve(replace(GRANT, run_id=OTHER_RUN, deadline_boottime=150.0))
    before = rows(journal)
    late = ClockReading(BOOT, 400.0, 1_000.0)
    expected = replace(GRANT, deadline_boottime=1_000.0)
    assert claim(journal, expected=expected, now=late) == refused(Refusal.ACTIVE_PRESENT)
    assert rows(journal) == before
    assert before[0][7] == "reserved"


@pytest.mark.parametrize("state", ["stopped", "quarantined", "stopping", "reserved"])
def test_any_existing_row_for_the_run_refuses(journal: Path, state: str) -> None:
    with closing(sqlite3.connect(journal)) as conn, conn:
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 0, 'never_claimed', 1)",
            (RUN, "x" * 64, PRINCIPAL, UID, BOOT, 700.0, 1600.0, state),
        )
    assert claim(journal) == refused(Refusal.RUN_ALREADY_RECORDED)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("fence", "bad fence!"),
        ("fence", "x" * 31),
        ("fence", "x" * 129),
        ("principal", "nobody"),
        ("uid", 0),
        ("boot_id", BOOT.upper()),
        ("deadline_utc", -1.0),
        ("reason", "r" * 257),
        ("deadline_boottime", "late"),
    ],
)
def test_malformed_existing_row_refuses_as_row(journal: Path, column: str, value: object) -> None:
    with closing(sqlite3.connect(journal)) as conn, conn:
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, 'stopped', 0, 1, 0, NULL, 1)",
            (RUN, FENCE, PRINCIPAL, UID, BOOT, 700.0, 1600.0),
        )
        conn.execute(f"UPDATE runs SET {column}=?", (value,))
    assert claim(journal) == refused(Refusal.ROW_REFUSED)


def test_shared_uid_principals_are_allowed(journal: Path) -> None:
    principals = {PRINCIPAL: UID, "other@huginn": UID}
    assert claim(journal, principals=principals).usable


@pytest.mark.parametrize(
    "principals",
    [{"other@huginn": UID}, {PRINCIPAL: UID + 1}],
    ids=["missing", "mismatch"],
)
def test_principal_binding(journal: Path, principals: dict[str, int]) -> None:
    before = journal.read_bytes()
    assert claim(journal, principals=principals) == refused(Refusal.PRINCIPAL_BINDING)
    assert journal.read_bytes() == before


def test_boot_mismatch(journal: Path) -> None:
    assert claim(journal, now=ClockReading(OTHER_BOOT, 100.0, 1_000.0)) == refused(Refusal.BOOT_MISMATCH)


@pytest.mark.parametrize(
    ("boottime", "utc", "grace", "accepted"),
    [
        (100.0 + 11.0 + 1e-9, 1_000.0 + 600.0, 5.0, True),
        (100.0 + 11.0, 1_000.0 + 600.0, 5.0, False),
        (100.0 + 600.0, 1_000.0 + 11.0, 5.0, False),
        (100.0 + 600.0, 1_000.0 + 11.5, 5.0, True),
        (100.0 + 1200.0, 1_000.0 + 1200.0, 5.0, True),
        (100.0 + 1200.0 + 1e-9, 1_000.0 + 600.0, 5.0, False),
        (100.0 + 600.0, 1_000.0 + 1200.0 + 1e-6, 5.0, False),
        (100.0 + 600.0, 1_000.0 + 600.0, 594.0 - 1e-9, True),
        (100.0 + 600.0, 1_000.0 + 600.0, 594.0, False),
        (100.0 + 7.0, 1_000.0 + 600.0, 0.5, True),
        (100.0 + 6.5, 1_000.0 + 600.0, 0.5, False),
    ],
)
def test_grant_window_edges(journal: Path, boottime: float, utc: float, grace: float, accepted: bool) -> None:
    expected = replace(GRANT, deadline_boottime=boottime, deadline_utc=utc)
    outcome = claim(journal, expected=expected, grace=grace)
    assert outcome == (result(C) if accepted else refused(Refusal.GRANT_WINDOW_REFUSED))


def _record() -> Record:
    return Record(RUN, FENCE, PRINCIPAL, UID, BOOT, 700.0, 1_600.0, "starting", True, False, False, None)


def _forged(**changes: Any) -> Grant:
    grant = replace(GRANT)
    for name, value in changes.items():
        object.__setattr__(grant, name, value)
    return grant


def _clock(**changes: Any) -> ClockReading:
    clock = replace(NOW)
    for name, value in changes.items():
        object.__setattr__(clock, name, value)
    return clock


class _Key(str):
    pass


INVALID: list[tuple[str, dict[str, Any]]] = [
    ("record", {"expected": _record()}),
    ("not-grant", {"expected": None}),
    ("missing-field", {"expected": object.__new__(Grant)}),
    ("fence-32", {"expected": replace(GRANT, fence="F" * 32)}),
    ("fence-44", {"expected": replace(GRANT, fence="F" * 44)}),
    ("fence-alphabet", {"expected": _forged(fence="F" * 42 + "!")}),
    ("run-upper", {"expected": _forged(run_id=RUN.upper())}),
    ("boot-upper", {"expected": _forged(boot_id=BOOT.upper())}),
    ("principal-long", {"expected": _forged(principal="p" * 120 + "@huginnxx")}),
    ("principal-grammar", {"expected": _forged(principal="pi")}),
    ("principal-non-ascii", {"expected": _forged(principal="pé@huginn")}),
    ("uid-bool", {"expected": _forged(uid=True)}),
    ("uid-zero", {"expected": _forged(uid=0)}),
    ("uid-overflow", {"expected": _forged(uid=2**63)}),
    ("deadline-int", {"expected": replace(GRANT, deadline_boottime=700)}),
    ("deadline-zero", {"expected": _forged(deadline_utc=0.0)}),
    ("deadline-nan", {"expected": _forged(deadline_utc=float("nan"))}),
    ("deadline-inf", {"expected": _forged(deadline_boottime=float("inf"))}),
    ("clock-type", {"now": (BOOT, 100.0, 1_000.0)}),
    ("clock-int", {"now": ClockReading(BOOT, 100, 1_000.0)}),
    ("clock-negative", {"now": _clock(utc=-1.0)}),
    ("clock-boot", {"now": _clock(boot_id="x")}),
    ("grace-int", {"grace": 5}),
    ("grace-zero", {"grace": 0.0}),
    ("grace-negative", {"grace": -5.0}),
    ("grace-nan", {"grace": float("nan")}),
    ("grace-bool", {"grace": True}),
    ("principals-empty", {"principals": {}}),
    ("principals-ordered", {"principals": OrderedDict(PRINCIPALS)}),
    ("principals-tuple", {"principals": ((PRINCIPAL, UID),)}),
    ("principals-65", {"principals": {f"p{i}@huginn": UID for i in range(64)} | {PRINCIPAL: UID}}),
    ("principals-key-subclass", {"principals": {_Key(PRINCIPAL): UID}}),
    ("principals-key-grammar", {"principals": {PRINCIPAL: UID, "bad": UID}}),
    ("principals-value-bool", {"principals": {PRINCIPAL: True}}),
    ("principals-value-zero", {"principals": {PRINCIPAL: UID, "o@huginn": 0}}),
    ("principals-value-overflow", {"principals": {PRINCIPAL: 2**63}}),
]


@pytest.mark.parametrize("changes", [case[1] for case in INVALID], ids=[case[0] for case in INVALID])
def test_invalid_input_refuses_before_any_file_access(changes: dict[str, Any], tmp_path: Path) -> None:
    missing = Path.home() / "js-never-created" / "journal.sqlite3"
    assert claim(missing, **changes) == refused(Refusal.INVALID_INPUT)
    assert not missing.parent.exists()


def test_sixty_four_principals_are_accepted(journal: Path) -> None:
    principals = {f"p{i}@huginn": UID for i in range(63)} | {PRINCIPAL: UID}
    assert claim(journal, principals=principals).usable


@pytest.mark.parametrize("path", ["/tmp/x", Path("relative"), b"/tmp/x"])
def test_invalid_path_type_or_shape(path: object) -> None:
    outcome = claim_start(path, expected=GRANT, principals=PRINCIPALS, now=NOW, grace=GRACE)  # type: ignore[arg-type]
    assert outcome.refusal in (Refusal.INVALID_INPUT, Refusal.PATH_REFUSED)
    assert outcome.mutation is N


def test_structural_validation_precedes_semantic_refusals(journal: Path) -> None:
    mismatch = ClockReading(OTHER_BOOT, 100.0, 1_000.0)
    assert claim(journal, now=mismatch, grace=0) == refused(Refusal.INVALID_INPUT)
    assert claim(journal, now=mismatch, principals={"o@huginn": UID}) == refused(Refusal.PRINCIPAL_BINDING)
    late = replace(GRANT, deadline_boottime=100.0 + 3.0)
    assert claim(journal, expected=late, now=_clock(boot_id=OTHER_BOOT)) == refused(Refusal.BOOT_MISMATCH)


def test_semantic_refusals_precede_file_checks() -> None:
    missing = Path.home() / "js-never-created" / "journal.sqlite3"
    assert claim(missing, principals={"o@huginn": UID}) == refused(Refusal.PRINCIPAL_BINDING)
    assert claim(missing, now=_clock(boot_id=OTHER_BOOT)) == refused(Refusal.BOOT_MISMATCH)
    assert claim(missing, grace=1000.0) == refused(Refusal.GRANT_WINDOW_REFUSED)
    assert claim(missing) == refused(Refusal.PATH_REFUSED)
    assert not missing.parent.exists()


def test_inputs_mutated_during_open_do_not_change_the_snapshot(
    journal: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant, principals = replace(GRANT), dict(PRINCIPALS)
    original = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        object.__setattr__(grant, "fence", "G" * 43)
        object.__setattr__(grant, "deadline_utc", 1_700.0)
        principals[PRINCIPAL] = UID + 7
        principals["new@huginn"] = UID
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert claim(journal, expected=grant, principals=principals).usable
    assert rows(journal)[0][1:7] == (FENCE, PRINCIPAL, UID, BOOT, 700.0, 1600.0)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_sidecar_presence_refuses(journal: Path, suffix: str) -> None:
    sidecar = journal.with_name(journal.name + suffix)
    sidecar.write_bytes(b"")
    before = journal.read_bytes()
    assert claim(journal) == refused(Refusal.SIDECAR_PRESENT)
    assert journal.read_bytes() == before
    assert sidecar.exists()


@pytest.mark.parametrize("mode", [0o640, 0o400, 0o700, 0o604])
def test_mode_other_than_exact_0600_refuses(journal: Path, mode: int) -> None:
    journal.chmod(mode)
    assert claim(journal) == refused(Refusal.PATH_REFUSED)


def test_missing_file_is_not_created(journal: Path) -> None:
    other = journal.with_name("absent.sqlite3")
    assert claim(other) == refused(Refusal.PATH_REFUSED)
    assert not other.exists()


def test_symlink_and_hardlink_refuse(journal: Path) -> None:
    link = journal.with_name("link.sqlite3")
    link.symlink_to(journal)
    assert claim(link) == refused(Refusal.PATH_REFUSED)
    link.unlink()
    os.link(journal, link)
    assert claim(journal) == refused(Refusal.PATH_REFUSED)


def test_unnormalized_path_refuses(journal: Path) -> None:
    alias = Path(f"{journal.parent}/../{journal.parent.name}/{journal.name}")
    assert claim(alias) == refused(Refusal.PATH_REFUSED)


def test_non_sqlite_header_refuses(journal: Path) -> None:
    journal.write_bytes(b"x" * 4096)
    assert claim(journal) == refused(Refusal.PATH_REFUSED)


def test_file_swapped_before_connect_refuses(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = journal.with_name("other.sqlite3")
    legacy(other)
    original = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        os.replace(other, journal)
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert claim(journal) == refused(Refusal.PATH_REFUSED)


@pytest.mark.parametrize(
    "statement",
    [
        "PRAGMA user_version=3",
        "CREATE TABLE extra(x)",
        "CREATE INDEX extra ON runs(principal)",
        "DROP INDEX one_active_run",
    ],
)
def test_schema_changes_refuse(journal: Path, statement: str) -> None:
    with closing(sqlite3.connect(journal)) as conn:
        conn.execute(statement)
    assert claim(journal) == refused(Refusal.SCHEMA_REFUSED)


def test_wal_journal_refuses_without_change(journal: Path) -> None:
    with closing(sqlite3.connect(journal)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    before = journal.read_bytes()
    assert claim(journal).refusal in (Refusal.PATH_REFUSED, Refusal.SIDECAR_PRESENT, Refusal.SCHEMA_REFUSED)
    assert journal.read_bytes() == before


HOLDER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute(sys.argv[2])
conn.execute("SELECT count(*) FROM runs").fetchall()
print("held", flush=True)
sys.stdin.readline()
conn.execute("ROLLBACK")
conn.close()
"""


def _holding(path: Path, begin: str) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER, str(path), begin],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline() == "held\n"
    return holder


def _release(holder: subprocess.Popen[str]) -> None:
    assert holder.stdin is not None
    holder.stdin.write("\n")
    holder.stdin.flush()
    assert holder.wait(timeout=30) == 0


def test_real_exclusive_holder_is_a_busy_refusal(journal: Path) -> None:
    holder = _holding(journal, "BEGIN EXCLUSIVE")
    try:
        outcome = claim(journal)
    finally:
        _release(holder)
    assert outcome == refused(Refusal.BUSY)
    assert rows(journal) == []


def test_real_shared_holder_through_commit_is_unknown(journal: Path) -> None:
    holder = _holding(journal, "BEGIN")
    try:
        outcome = claim(journal)
    finally:
        _release(holder)
    assert outcome == result(U, fault=Fault.SQLITE_FAILURE)
    assert not outcome.usable


def _contract_rows() -> set[tuple[MutationOutcome, CleanupOutcome, Refusal | None, Fault | None]]:
    allowed: set[tuple[MutationOutcome, CleanupOutcome, Refusal | None, Fault | None]] = set()
    cleanup_faults = (Fault.ROLLBACK_FAILURE, Fault.CLOSE_FAILURE)
    for refusal in Refusal:
        allowed.add((N, CLOSED, refusal, None))
        for fault in cleanup_faults:
            allowed.add((N, UNCERTAIN, refusal, fault))
    for fault in (Fault.FILE_FAILURE, Fault.SQLITE_FAILURE):
        allowed.add((N, CLOSED, None, fault))
    for fault in cleanup_faults:
        allowed.add((N, UNCERTAIN, None, fault))
        allowed.add((U, UNCERTAIN, None, fault))
    for fault in (Fault.SQLITE_FAILURE, Fault.SNAPSHOT_FAILURE):
        allowed.add((RB, CLOSED, None, fault))
    allowed.add((RB, UNCERTAIN, None, Fault.CLOSE_FAILURE))
    allowed.add((U, CLOSED, None, Fault.SQLITE_FAILURE))
    allowed.add((C, UNCERTAIN, None, Fault.CLOSE_FAILURE))
    allowed.add((C, CLOSED, None, None))
    return allowed


def test_result_constructor_accepts_exactly_the_contract_table() -> None:
    allowed = _contract_rows()
    for combination in itertools.product(MutationOutcome, CleanupOutcome, [None, *Refusal], [None, *Fault]):
        if combination in allowed:
            built = StartClaimResult(*combination)
            assert built.usable == (combination == (C, CLOSED, None, None))
        else:
            with pytest.raises(ValueError, match="inconsistent"):
                StartClaimResult(*combination)


@pytest.mark.parametrize(
    "values",
    [
        ("committed", CLOSED, None, None),
        (C, "closed", None, None),
        (N, CLOSED, "BUSY", None),
        (N, CLOSED, None, "FILE_FAILURE"),
        (N, CLOSED, True, None),
    ],
)
def test_result_constructor_rejects_wrong_types(values: tuple[Any, ...]) -> None:
    with pytest.raises(TypeError):
        StartClaimResult(*values)


def test_result_has_no_success_field_or_authority_payload() -> None:
    assert StartClaimResult.__slots__ == ("mutation", "cleanup", "refusal", "fault")
    with pytest.raises((AttributeError, TypeError)):
        result(C).usable = False  # type: ignore[misc]
