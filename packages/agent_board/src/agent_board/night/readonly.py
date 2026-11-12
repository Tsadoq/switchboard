import json
import math
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_board.night.ledger import LEDGER_VERSION, CapturedAssignment, Identity, RecordedBinding
from agent_board.night_inspection import unit_cgroup
from agent_board.night_state import Record, StateError, _record, check_private_file

JOURNAL_VERSION = 2
JOURNAL_TABLES: Mapping[str, tuple[str, ...]] = {
    "runs": (
        "run_id",
        "fence",
        "principal",
        "uid",
        "boot_id",
        "deadline_boottime",
        "deadline_utc",
        "state",
        "launch_claimed",
        "stop_requested",
        "uncertain",
        "reason",
        "slot",
    ),
}
LEDGER_TABLES: Mapping[str, tuple[str, ...]] = {
    "identity": ("singleton", "journal_id", "acked_counter"),
    "receipts": ("seq", "receipt_id", "action", "run_id", "payload", "acked"),
    "bindings": ("run_id", "uid", "boot_id", "invocation_id", "cgroup", "server_acked"),
    "arms": ("run_id", "guard_boot_id", "guard_invocation_id", "deadline_boottime", "deadline_utc"),
    "captures": ("run_id", "receipt_id", "principal", "cutoff_utc", "policy", "snapshot", "assignment"),
    "capture_aborts": ("parent_receipt_id", "abort_receipt_id", "outcome", "proof"),
}


class ReadOnlyError(StateError):
    pass


@dataclass(frozen=True)
class BoundRun:
    record: Record
    binding: RecordedBinding
    capture: CapturedAssignment


class ReadOnlyJournal:
    """The journal's rows exactly as stored. It never refreshes expiry the way LocalJournal.active() does,
    since that writes; callers judge deadlines themselves against their own clock."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def runs(self) -> tuple[Record, ...]:
        with _read_only(self._path, JOURNAL_VERSION, JOURNAL_TABLES) as conn:
            try:
                return tuple(
                    _record(row) for row in conn.execute("SELECT * FROM runs WHERE state != 'stopped'")
                )
            except (StateError, TypeError, ValueError):
                raise ReadOnlyError("journal_row_corrupt") from None


class ReadOnlyLedger:
    def __init__(self, path: Path) -> None:
        self._path = path

    def identity(self) -> Identity:
        with _read_only(self._path, LEDGER_VERSION, LEDGER_TABLES) as conn:
            row = conn.execute("SELECT journal_id, acked_counter FROM identity").fetchone()
            if row is None:
                raise ReadOnlyError("ledger_identity_missing")
            journal_id, counter = row["journal_id"], row["acked_counter"]
            if (
                (journal_id is not None and not isinstance(journal_id, str))
                or type(counter) is not int
                or counter < 0
            ):
                raise ReadOnlyError("ledger_row_corrupt")
            return Identity(journal_id, counter)

    def binding(self, run_id: str) -> RecordedBinding | None:
        with _read_only(self._path, LEDGER_VERSION, LEDGER_TABLES) as conn:
            row = conn.execute("SELECT * FROM bindings WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            if (
                type(row["uid"]) is not int
                or row["server_acked"] not in (0, 1)
                or type(row["server_acked"]) is not int
                or not all(
                    isinstance(row[key], str) for key in ("run_id", "boot_id", "invocation_id", "cgroup")
                )
            ):
                raise ReadOnlyError("ledger_row_corrupt")
            return RecordedBinding(
                row["run_id"],
                row["uid"],
                row["boot_id"],
                row["invocation_id"],
                row["cgroup"],
                bool(row["server_acked"]),
            )

    def usable_capture(self, run_id: str) -> CapturedAssignment | None:
        """An acknowledged capture with an assignment and no abort link, or None."""
        with _read_only(self._path, LEDGER_VERSION, LEDGER_TABLES) as conn:
            row = conn.execute(
                "SELECT c.receipt_id, c.snapshot, c.assignment, r.acked, a.parent_receipt_id AS aborted "
                "FROM captures c JOIN receipts r ON r.receipt_id = c.receipt_id "
                "AND r.action = 'capture' AND r.run_id = c.run_id "
                "LEFT JOIN capture_aborts a ON a.parent_receipt_id = c.receipt_id WHERE c.run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row["acked"] != 1 or row["aborted"] is not None or row["assignment"] is None:
                return None
            if not isinstance(row["assignment"], bytes) or not _snapshot(row["snapshot"]):
                raise ReadOnlyError("ledger_row_corrupt")
            return CapturedAssignment(run_id, row["receipt_id"], row["snapshot"], row["assignment"])


def bound_run(
    journal: ReadOnlyJournal,
    ledger: ReadOnlyLedger,
    *,
    cgroup: str,
    invocation_id: str,
    boot_id: str,
    boottime: float,
    utc: float,
    grace: float,
) -> BoundRun:
    """The one run a peer in `cgroup` may act for, derived from root state alone; any doubt refuses.

    `cgroup`, `invocation_id` and `boot_id` are the kernel's and the manager's observations of the peer and
    its unit, never values the peer supplied.
    """
    matches = [
        record
        for record in journal.runs()
        if unit_cgroup(f"switchboard-night-{record.run_id}.service") == cgroup
    ]
    if len(matches) != 1:
        raise ReadOnlyError("no_single_run_for_cgroup")
    record = matches[0]
    if (
        record.state != "starting"
        or not record.launch_claimed
        or record.stop_requested
        or record.uncertain
        or record.boot_id != boot_id
    ):
        raise ReadOnlyError("run_not_running")
    clock = [_finite(value) for value in (boottime, utc, grace)]
    if any(value is None for value in clock) or (clock[2] is not None and clock[2] < 0):
        raise ReadOnlyError("invalid_clock")
    if boottime >= record.deadline_boottime - grace or utc >= record.deadline_utc - grace:
        raise ReadOnlyError("run_past_deadline")
    binding = ledger.binding(record.run_id)
    if (
        binding is None
        or not binding.server_acked
        or binding.invocation_id != invocation_id
        or binding.boot_id != boot_id
        or binding.cgroup != cgroup
        or binding.uid != record.uid
    ):
        raise ReadOnlyError("binding_mismatch")
    capture = ledger.usable_capture(record.run_id)
    if capture is None:
        raise ReadOnlyError("no_usable_capture")
    return BoundRun(record, binding, capture)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _snapshot(text: object) -> bool:
    if not isinstance(text, str):
        return False
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return False
    return isinstance(value, dict) and isinstance(value.get("wakes"), list)


@contextmanager
def _read_only(
    path: Path, version: int, tables: Mapping[str, tuple[str, ...]]
) -> Iterator[sqlite3.Connection]:
    """A read-only snapshot after the private-file checks, refusing any schema but the expected one.

    `mode=ro` without `immutable=1`, so commits by the writer stay visible to every later read.
    """
    check_private_file(path)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        if conn.execute("PRAGMA user_version").fetchone()[0] != version:
            raise ReadOnlyError("unexpected_schema_version")
        present = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if row["name"] != "sqlite_sequence"
        }
        if present != set(tables):
            raise ReadOnlyError("unexpected_tables")
        for table, columns in tables.items():
            if tuple(row["name"] for row in conn.execute(f"PRAGMA table_info({table})")) != columns:
                raise ReadOnlyError("unexpected_columns")
        yield conn
    except sqlite3.Error:
        raise ReadOnlyError("state_unreadable") from None
    finally:
        if conn is not None:
            conn.close()
