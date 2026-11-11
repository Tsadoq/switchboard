import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_board.night_state import StateError, check_private_file, prepare_private_file

LEDGER_VERSION = 1
_SCHEMA = (
    (
        "CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), journal_id TEXT, "
        "acked_counter INTEGER NOT NULL CHECK(acked_counter >= 0))"
    ),
    "INSERT OR IGNORE INTO identity (singleton, journal_id, acked_counter) VALUES (1, NULL, 0)",
    (
        "CREATE TABLE IF NOT EXISTS receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL UNIQUE, "
        "action TEXT NOT NULL CHECK(action IN ('bootstrap', 'binding', 'evidence')), run_id TEXT, "
        "payload TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0 CHECK(acked IN (0, 1)))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS bindings (run_id TEXT PRIMARY KEY, boot_id TEXT NOT NULL, "
        "invocation_id TEXT NOT NULL, cgroup TEXT NOT NULL, "
        "server_acked INTEGER NOT NULL DEFAULT 0 CHECK(server_acked IN (0, 1)))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS arms (run_id TEXT PRIMARY KEY, guard_boot_id TEXT NOT NULL, "
        "guard_invocation_id TEXT NOT NULL, deadline_boottime REAL NOT NULL, deadline_utc REAL NOT NULL)"
    ),
)

type Action = Literal["bootstrap", "binding", "evidence"]


class LedgerError(StateError):
    pass


@dataclass(frozen=True)
class Identity:
    journal_id: str | None
    acked_counter: int


@dataclass(frozen=True)
class Pending:
    receipt_id: str
    action: Action
    run_id: str | None
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class Arm:
    run_id: str
    guard_boot_id: str
    guard_invocation_id: str
    deadline_boottime: float
    deadline_utc: float


class Ledger:
    """What the controller has told the board, and what the board and the guard have acknowledged.

    A message is persisted whole before it is sent, so a retry after a crash resends the identical
    payload under the same receipt id instead of composing a new one.
    """

    def __init__(self, path: Path) -> None:
        prepare_private_file(path)
        self._path = path
        with self._transaction(initialize=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, LEDGER_VERSION):
                raise LedgerError("unsupported_ledger_version")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {LEDGER_VERSION}")

    def identity(self) -> Identity:
        with self._transaction() as conn:
            row = conn.execute("SELECT journal_id, acked_counter FROM identity").fetchone()
            return Identity(row["journal_id"], row["acked_counter"])

    def persist(self, pending: Pending) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO receipts (receipt_id, action, run_id, payload) VALUES (?, ?, ?, ?)",
                (
                    pending.receipt_id,
                    pending.action,
                    pending.run_id,
                    json.dumps(dict(pending.payload), sort_keys=True),
                ),
            )

    def unacknowledged(self) -> tuple[Pending, ...]:
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM receipts WHERE acked = 0 ORDER BY seq").fetchall()
            return tuple(
                Pending(row["receipt_id"], row["action"], row["run_id"], json.loads(row["payload"]))
                for row in rows
            )

    def acknowledge(self, receipt_id: str, *, journal_id: str, counter: int) -> None:
        with self._transaction() as conn:
            current = conn.execute("SELECT journal_id, acked_counter FROM identity").fetchone()
            if current["journal_id"] not in (None, journal_id):
                raise LedgerError("journal_identity_mismatch")
            if counter < current["acked_counter"]:
                raise LedgerError("counter_regressed")
            updated = conn.execute(
                "UPDATE receipts SET acked = 1 WHERE receipt_id = ? AND acked = 0", (receipt_id,)
            )
            if updated.rowcount != 1:
                raise LedgerError("unknown_receipt")
            conn.execute("UPDATE identity SET journal_id = ?, acked_counter = ?", (journal_id, counter))
            pending = conn.execute(
                "SELECT action, run_id FROM receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if pending["action"] == "binding":
                conn.execute("UPDATE bindings SET server_acked = 1 WHERE run_id = ?", (pending["run_id"],))

    def record_binding(self, run_id: str, *, boot_id: str, invocation_id: str, cgroup: str) -> None:
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM bindings WHERE run_id = ?", (run_id,)).fetchone()
            if existing is not None:
                if (existing["boot_id"], existing["invocation_id"], existing["cgroup"]) != (
                    boot_id,
                    invocation_id,
                    cgroup,
                ):
                    raise LedgerError("binding_conflict")
                return
            conn.execute(
                "INSERT INTO bindings (run_id, boot_id, invocation_id, cgroup) VALUES (?, ?, ?, ?)",
                (run_id, boot_id, invocation_id, cgroup),
            )

    def arm(self, arm: Arm) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO arms (run_id, guard_boot_id, guard_invocation_id, deadline_boottime, deadline_utc) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (run_id) DO UPDATE SET "
                "guard_boot_id = excluded.guard_boot_id, guard_invocation_id = excluded.guard_invocation_id, "
                "deadline_boottime = excluded.deadline_boottime, deadline_utc = excluded.deadline_utc",
                (
                    arm.run_id,
                    arm.guard_boot_id,
                    arm.guard_invocation_id,
                    arm.deadline_boottime,
                    arm.deadline_utc,
                ),
            )

    def ready(
        self,
        run_id: str,
        *,
        boot_id: str,
        guard_invocation_id: str,
        deadline_boottime: float,
        deadline_utc: float,
    ) -> bool:
        """True only when the board recorded this run's binding and the running guard armed its exact deadlines."""
        with self._transaction() as conn:
            bound = conn.execute(
                "SELECT 1 FROM bindings WHERE run_id = ? AND boot_id = ? AND server_acked = 1",
                (run_id, boot_id),
            ).fetchone()
            armed = conn.execute(
                "SELECT 1 FROM arms WHERE run_id = ? AND guard_boot_id = ? AND guard_invocation_id = ? "
                "AND deadline_boottime = ? AND deadline_utc = ?",
                (run_id, boot_id, guard_invocation_id, deadline_boottime, deadline_utc),
            ).fetchone()
            return bound is not None and armed is not None

    @contextmanager
    def _transaction(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        conn: sqlite3.Connection | None = None
        try:
            check_private_file(self._path)
            conn = sqlite3.connect(self._path, isolation_level=None, timeout=5)
            conn.row_factory = sqlite3.Row
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise LedgerError("unsupported_journal_mode")
            conn.execute("PRAGMA synchronous = EXTRA")
            conn.execute("BEGIN IMMEDIATE")
            if not initialize and conn.execute("PRAGMA user_version").fetchone()[0] != LEDGER_VERSION:
                raise LedgerError("unsupported_ledger_version")
            yield conn
            conn.commit()
        except sqlite3.Error:
            raise LedgerError("ledger_unavailable") from None
        finally:
            if conn is not None:
                conn.close()
