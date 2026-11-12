import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from agent_board.night.assignment import NightPolicy, policy_from
from agent_board.night_state import StateError, check_private_file, prepare_private_file

LEDGER_VERSION = 3
_RECEIPTS = (
    "CREATE TABLE IF NOT EXISTS receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL UNIQUE, "
    "action TEXT NOT NULL CHECK(action IN ('bootstrap', 'binding', 'evidence', 'capture', 'capture_abort')), "
    "run_id TEXT, payload TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0 CHECK(acked IN (0, 1)))"
)
_REBUILD_RECEIPTS = (
    "ALTER TABLE receipts RENAME TO receipts_v1",
    _RECEIPTS,
    (
        "INSERT INTO receipts (seq, receipt_id, action, run_id, payload, acked) "
        "SELECT seq, receipt_id, action, run_id, payload, acked FROM receipts_v1"
    ),
    "DROP TABLE receipts_v1",
)
_SCHEMA = (
    (
        "CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), journal_id TEXT, "
        "acked_counter INTEGER NOT NULL CHECK(acked_counter >= 0))"
    ),
    "INSERT OR IGNORE INTO identity (singleton, journal_id, acked_counter) VALUES (1, NULL, 0)",
    _RECEIPTS,
    ("CREATE UNIQUE INDEX IF NOT EXISTS one_evidence_per_run ON receipts (run_id) WHERE action = 'evidence'"),
    ("CREATE UNIQUE INDEX IF NOT EXISTS one_capture_per_run ON receipts (run_id) WHERE action = 'capture'"),
    (
        "CREATE TABLE IF NOT EXISTS capture_aborts (parent_receipt_id TEXT PRIMARY KEY, "
        "abort_receipt_id TEXT NOT NULL UNIQUE, outcome TEXT CHECK(outcome IN ('aborted', 'applied')), "
        "proof TEXT, CHECK((outcome IS NULL) = (proof IS NULL)))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS captures (run_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE, "
        "principal TEXT NOT NULL, cutoff_utc REAL NOT NULL, policy TEXT NOT NULL, snapshot TEXT, "
        "assignment BLOB, CHECK(assignment IS NULL OR snapshot IS NOT NULL))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS bindings (run_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, boot_id TEXT NOT NULL, "
        "invocation_id TEXT NOT NULL, cgroup TEXT NOT NULL, "
        "server_acked INTEGER NOT NULL DEFAULT 0 CHECK(server_acked IN (0, 1)))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS arms (run_id TEXT PRIMARY KEY, guard_boot_id TEXT NOT NULL, "
        "guard_invocation_id TEXT NOT NULL, deadline_boottime REAL NOT NULL, deadline_utc REAL NOT NULL)"
    ),
)

type Action = Literal["bootstrap", "binding", "evidence", "capture", "capture_abort"]


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
class RecordedBinding:
    run_id: str
    uid: int
    boot_id: str
    invocation_id: str
    cgroup: str
    server_acked: bool


@dataclass(frozen=True)
class CaptureContext:
    """Fixed when the capture receipt is persisted, so a retry builds the assignment from the same choices."""

    principal: str
    cutoff_utc: float
    policy: NightPolicy


@dataclass(frozen=True)
class CapturedAssignment:
    """An acknowledged capture: the server's snapshot and the assignment derived from it, None when empty.

    `aborted_after_apply` marks a capture recovered through an abort that found it already applied: its run
    was being stopped, so its assignment must never be launched.
    """

    run_id: str
    receipt_id: str
    snapshot: str
    assignment: bytes | None
    aborted_after_apply: bool = False


@dataclass(frozen=True)
class CaptureAborted:
    """A capture the board proved it never applied; it can never yield an assignment."""

    run_id: str
    receipt_id: str
    proof: str


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
            if version not in (0, 1, 2, LEDGER_VERSION):
                raise LedgerError("unsupported_ledger_version")
            if version in (1, 2):
                for statement in _REBUILD_RECEIPTS:
                    conn.execute(statement)
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

    def persist_capture(self, pending: Pending, context: CaptureContext) -> None:
        """The capture receipt and the choices its assignment will use, in one transaction."""
        if pending.action != "capture" or pending.run_id is None:
            raise LedgerError("not_a_capture")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM captures WHERE run_id = ?", (pending.run_id,)).fetchone():
                raise LedgerError("capture_exists")
            conn.execute(
                "INSERT INTO receipts (receipt_id, action, run_id, payload) VALUES (?, 'capture', ?, ?)",
                (pending.receipt_id, pending.run_id, json.dumps(dict(pending.payload), sort_keys=True)),
            )
            conn.execute(
                "INSERT INTO captures (run_id, receipt_id, principal, cutoff_utc, policy) VALUES (?, ?, ?, ?, ?)",
                (
                    pending.run_id,
                    pending.receipt_id,
                    context.principal,
                    context.cutoff_utc,
                    json.dumps(asdict(context.policy), sort_keys=True),
                ),
            )

    def capture_context(self, receipt_id: str) -> CaptureContext:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT principal, cutoff_utc, policy FROM captures WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("unknown_capture")
            return CaptureContext(row["principal"], row["cutoff_utc"], policy_from(json.loads(row["policy"])))

    def captured(self, run_id: str) -> CapturedAssignment | CaptureAborted | Pending | None:
        """The run's resolved capture, the receipt still to send for it, or None if none was composed.

        Once an abort is linked, the receipt to send is the abort, never its parent capture again.
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT c.receipt_id, c.snapshot, c.assignment, r.payload, r.acked, a.abort_receipt_id, a.outcome, "
                "a.proof, ar.payload AS abort_payload FROM captures c "
                "JOIN receipts r ON r.receipt_id = c.receipt_id "
                "LEFT JOIN capture_aborts a ON a.parent_receipt_id = c.receipt_id "
                "LEFT JOIN receipts ar ON ar.receipt_id = a.abort_receipt_id WHERE c.run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            if row["outcome"] == "aborted":
                return CaptureAborted(run_id, row["receipt_id"], row["proof"])
            if row["abort_receipt_id"] is not None and row["outcome"] is None:
                return Pending(
                    row["abort_receipt_id"], "capture_abort", run_id, json.loads(row["abort_payload"])
                )
            if not row["acked"]:
                return Pending(row["receipt_id"], "capture", run_id, json.loads(row["payload"]))
            return CapturedAssignment(
                run_id, row["receipt_id"], row["snapshot"], row["assignment"], row["outcome"] == "applied"
            )

    def persist_abort(self, parent_receipt_id: str, abort: Pending) -> None:
        """The abort receipt and its link to the unacknowledged parent capture, in one transaction."""
        if abort.action != "capture_abort" or abort.run_id is None:
            raise LedgerError("not_an_abort")
        with self._transaction() as conn:
            parent = conn.execute(
                "SELECT run_id, acked FROM receipts WHERE receipt_id = ? AND action = 'capture'",
                (parent_receipt_id,),
            ).fetchone()
            if parent is None or parent["acked"] or parent["run_id"] != abort.run_id:
                raise LedgerError("abort_parent_not_pending")
            if conn.execute(
                "SELECT 1 FROM capture_aborts WHERE parent_receipt_id = ?", (parent_receipt_id,)
            ).fetchone():
                raise LedgerError("abort_exists")
            conn.execute(
                "INSERT INTO receipts (receipt_id, action, run_id, payload) VALUES (?, 'capture_abort', ?, ?)",
                (abort.receipt_id, abort.run_id, json.dumps(dict(abort.payload), sort_keys=True)),
            )
            conn.execute(
                "INSERT INTO capture_aborts (parent_receipt_id, abort_receipt_id) VALUES (?, ?)",
                (parent_receipt_id, abort.receipt_id),
            )

    def abort_parent(self, abort_receipt_id: str) -> Pending:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT r.receipt_id, r.run_id, r.payload FROM capture_aborts a "
                "JOIN receipts r ON r.receipt_id = a.parent_receipt_id WHERE a.abort_receipt_id = ?",
                (abort_receipt_id,),
            ).fetchone()
            if row is None:
                raise LedgerError("unknown_abort")
            return Pending(row["receipt_id"], "capture", row["run_id"], json.loads(row["payload"]))

    def resolve_abort(
        self,
        abort_receipt_id: str,
        *,
        journal_id: str,
        counter: int,
        proof: str,
        applied: tuple[str, bytes | None] | None,
    ) -> None:
        """Resolves the abort and its parent together: aborted acknowledges only the abort, while applied
        acknowledges the parent with its snapshot and assignment and supersedes the abort."""
        with self._transaction() as conn:
            current = conn.execute("SELECT journal_id, acked_counter FROM identity").fetchone()
            if current["journal_id"] != journal_id:
                raise LedgerError("journal_identity_mismatch")
            if counter < current["acked_counter"]:
                raise LedgerError("counter_regressed")
            link = conn.execute(
                "SELECT parent_receipt_id FROM capture_aborts WHERE abort_receipt_id = ? AND outcome IS NULL",
                (abort_receipt_id,),
            ).fetchone()
            if link is None:
                raise LedgerError("unknown_abort")
            parent = link["parent_receipt_id"]
            acked = conn.execute(
                "UPDATE receipts SET acked = 1 WHERE receipt_id = ? AND acked = 0", (abort_receipt_id,)
            )
            if acked.rowcount != 1:
                raise LedgerError("unknown_abort")
            if applied is not None:
                snapshot, assignment = applied
                if (
                    conn.execute(
                        "UPDATE receipts SET acked = 1 WHERE receipt_id = ? AND acked = 0", (parent,)
                    ).rowcount
                    != 1
                ):
                    raise LedgerError("abort_parent_not_pending")
                stored = conn.execute(
                    "UPDATE captures SET snapshot = ?, assignment = ? WHERE receipt_id = ? AND snapshot IS NULL",
                    (snapshot, assignment, parent),
                )
                if stored.rowcount != 1:
                    raise LedgerError("unknown_capture")
            conn.execute(
                "UPDATE capture_aborts SET outcome = ?, proof = ? WHERE abort_receipt_id = ?",
                ("applied" if applied is not None else "aborted", proof, abort_receipt_id),
            )
            conn.execute("UPDATE identity SET acked_counter = ?", (counter,))

    def unacknowledged(self) -> tuple[Pending, ...]:
        """Receipts still to send, in order; a capture with a linked abort is replaced by that abort."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM receipts WHERE acked = 0 AND receipt_id NOT IN "
                "(SELECT parent_receipt_id FROM capture_aborts) ORDER BY seq"
            ).fetchall()
            return tuple(
                Pending(row["receipt_id"], row["action"], row["run_id"], json.loads(row["payload"]))
                for row in rows
            )

    def evidence(self, run_id: str) -> tuple[Pending, bool] | None:
        """The run's evidence message and whether the board acknowledged it, if one was ever composed."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM receipts WHERE action = 'evidence' AND run_id = ? ORDER BY seq LIMIT 1",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            pending = Pending(row["receipt_id"], row["action"], row["run_id"], json.loads(row["payload"]))
            return pending, bool(row["acked"])

    def acknowledge(
        self,
        receipt_id: str,
        *,
        journal_id: str,
        counter: int,
        capture: tuple[str, bytes | None] | None = None,
    ) -> None:
        """Marks a receipt acknowledged and moves the counter; a capture's snapshot and derived assignment are
        stored in the same transaction, so no acknowledged capture can exist without them."""
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
            if (pending["action"] == "capture") != (capture is not None):
                raise LedgerError("capture_acknowledgement_mismatch")
            if capture is not None:
                snapshot, assignment = capture
                stored = conn.execute(
                    "UPDATE captures SET snapshot = ?, assignment = ? WHERE receipt_id = ? AND snapshot IS NULL",
                    (snapshot, assignment, receipt_id),
                )
                if stored.rowcount != 1:
                    raise LedgerError("unknown_capture")

    def record_binding(self, run_id: str, *, uid: int, boot_id: str, invocation_id: str, cgroup: str) -> None:
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM bindings WHERE run_id = ?", (run_id,)).fetchone()
            if existing is not None:
                if (existing["uid"], existing["boot_id"], existing["invocation_id"], existing["cgroup"]) != (
                    uid,
                    boot_id,
                    invocation_id,
                    cgroup,
                ):
                    raise LedgerError("binding_conflict")
                return
            conn.execute(
                "INSERT INTO bindings (run_id, uid, boot_id, invocation_id, cgroup) VALUES (?, ?, ?, ?, ?)",
                (run_id, uid, boot_id, invocation_id, cgroup),
            )

    def binding(self, run_id: str) -> RecordedBinding | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM bindings WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            return RecordedBinding(
                row["run_id"],
                row["uid"],
                row["boot_id"],
                row["invocation_id"],
                row["cgroup"],
                bool(row["server_acked"]),
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
