import math
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from agent_board.night_protocol import FrameError, Request

MIN_START_SECONDS = 6
MAX_RUN_SECONDS = 1200


class StateError(ValueError):
    pass


@dataclass(frozen=True)
class ClockReading:
    boot_id: str
    boottime: float
    utc: float

    def __post_init__(self) -> None:
        if not _uuid(self.boot_id) or not _number(self.boottime) or not _number(self.utc):
            raise StateError("invalid_clock")


def clock_reading() -> ClockReading:
    return ClockReading(
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        time.clock_gettime(time.CLOCK_BOOTTIME),
        time.time(),
    )


@dataclass(frozen=True)
class Grant:
    run_id: str
    fence: str = field(repr=False)
    principal: str
    uid: int
    boot_id: str
    deadline_boottime: float
    deadline_utc: float

    def __post_init__(self) -> None:
        _identity(self.run_id, self.fence)
        if (
            not isinstance(self.principal, str)
            or re.fullmatch(r"[A-Za-z0-9._-]+@[a-z0-9_-]+", self.principal) is None
        ):
            raise StateError("invalid_principal")
        if type(self.uid) is not int or self.uid <= 0:
            raise StateError("invalid_uid")
        if not _uuid(self.boot_id):
            raise StateError("invalid_boot")
        if not _number(self.deadline_boottime) or not _number(self.deadline_utc):
            raise StateError("invalid_deadline")


@dataclass(frozen=True)
class Record(Grant):
    state: str
    launch_claimed: bool
    stop_requested: bool
    uncertain: bool
    reason: str | None


@dataclass(frozen=True)
class Reservation:
    created: bool
    record: Record


class LocalJournal:
    def __init__(
        self,
        path: Path,
        *,
        box: str,
        principals: Mapping[str, int],
        clock: Callable[[], ClockReading] = clock_reading,
    ) -> None:
        if re.fullmatch(r"[a-z0-9_-]+", box) is None or box in ("ops", "heimdall") or not principals:
            raise StateError("invalid_configuration")
        for principal, uid in principals.items():
            if (
                re.fullmatch(r"[A-Za-z0-9._-]+@[a-z0-9_-]+", principal) is None
                or principal.partition("@")[2] != box
                or principal.partition("@")[0].casefold() == "verifier"
                or type(uid) is not int
                or uid <= 0
            ):
                raise StateError("invalid_configuration")
        self._path = path
        self._principals = dict(principals)
        self._clock = clock
        if not path.is_absolute() or path.anchor != "/" or path != Path(os.path.normpath(path)):
            raise StateError("invalid_state_path")
        _check_ancestors(path.parent, allow_missing=True)
        missing = [directory for directory in (path.parent, *path.parent.parents) if not directory.exists()]
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_ancestors(path.parent)
        for directory in reversed(missing):
            _sync_directory(directory.parent)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            _check_file(os.fstat(fd))
        finally:
            os.close(fd)
        _sync_directory(path.parent)
        with self._transaction(initialize=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 2):
                raise StateError("unsupported_state_version")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "run_id TEXT PRIMARY KEY, fence TEXT NOT NULL, principal TEXT NOT NULL, uid INTEGER NOT NULL, "
                "boot_id TEXT NOT NULL, deadline_boottime REAL NOT NULL, deadline_utc REAL NOT NULL, "
                "state TEXT NOT NULL CHECK(state IN ('reserved', 'starting', 'stopping', 'quarantined', 'stopped')), "
                "launch_claimed INTEGER NOT NULL DEFAULT 0 CHECK(launch_claimed IN (0, 1)), "
                "stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0, 1)), "
                "uncertain INTEGER NOT NULL DEFAULT 0 CHECK(uncertain IN (0, 1)), reason TEXT, "
                "slot INTEGER NOT NULL DEFAULT 1 CHECK(slot = 1))"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs (slot) WHERE state != 'stopped'"
            )
            conn.execute("PRAGMA user_version = 2")
            self._refresh(conn, self._clock())

    def reserve(self, grant: Grant) -> Reservation:
        if self._principals.get(grant.principal) != grant.uid:
            raise StateError("principal_binding")
        with self._transaction() as conn:
            now = self._clock()
            self._refresh(conn, now)
            existing = conn.execute("SELECT * FROM runs WHERE run_id = ?", (grant.run_id,)).fetchone()
            if existing is not None:
                record = _record(existing)
                if not secrets.compare_digest(record.fence, grant.fence):
                    raise StateError("stale_fence")
                if (record.principal, record.uid, record.boot_id) != (
                    grant.principal,
                    grant.uid,
                    grant.boot_id,
                ):
                    raise StateError("run_binding")
                return Reservation(False, record)
            if conn.execute("SELECT 1 FROM runs WHERE state != 'stopped' LIMIT 1").fetchone() is not None:
                raise StateError("capacity")
            remaining = (grant.deadline_boottime - now.boottime, grant.deadline_utc - now.utc)
            if (
                grant.boot_id != now.boot_id
                or min(remaining) <= MIN_START_SECONDS
                or max(remaining) > MAX_RUN_SECONDS
            ):
                raise StateError("expired_or_invalid_grant")
            conn.execute(
                "INSERT INTO runs (run_id, fence, principal, uid, boot_id, deadline_boottime, deadline_utc, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved')",
                (
                    grant.run_id,
                    grant.fence,
                    grant.principal,
                    grant.uid,
                    grant.boot_id,
                    grant.deadline_boottime,
                    grant.deadline_utc,
                ),
            )
            return Reservation(True, self._fenced(conn, grant.run_id, grant.fence))

    def claim_start(self, run_id: str, fence: str) -> bool:
        with self._transaction() as conn:
            now = self._clock()
            self._refresh(conn, now)
            record = self._fenced(conn, run_id, fence)
            if record.state != "reserved" or record.launch_claimed:
                return False
            if (
                min(record.deadline_boottime - now.boottime, record.deadline_utc - now.utc)
                <= MIN_START_SECONDS
            ):
                conn.execute(
                    "UPDATE runs SET state = 'stopped', stop_requested = 1, reason = 'never_claimed_insufficient_time' WHERE run_id = ?",
                    (run_id,),
                )
                return False
            conn.execute("UPDATE runs SET state = 'starting', launch_claimed = 1 WHERE run_id = ?", (run_id,))
            return True

    def request_stop(self, run_id: str, fence: str) -> Record:
        with self._transaction() as conn:
            self._refresh(conn, self._clock())
            record = self._fenced(conn, run_id, fence)
            if record.state == "stopped":
                return record
            if not record.launch_claimed and not record.uncertain:
                conn.execute(
                    "UPDATE runs SET state = 'stopped', stop_requested = 1, reason = 'never_claimed' WHERE run_id = ?",
                    (run_id,),
                )
            else:
                conn.execute(
                    "UPDATE runs SET state = CASE WHEN state = 'quarantined' THEN state ELSE 'stopping' END, stop_requested = 1 WHERE run_id = ?",
                    (run_id,),
                )
            return self._fenced(conn, run_id, fence)

    def quarantine(self, run_id: str, fence: str, reason: str) -> Record:
        if reason not in ("launch_uncertain", "binding_conflict", "proof_incomplete"):
            raise StateError("invalid_reason")
        with self._transaction() as conn:
            record = self._fenced(conn, run_id, fence)
            if record.state == "stopped":
                return record
            conn.execute(
                "UPDATE runs SET state = 'quarantined', uncertain = 1, stop_requested = 1, reason = COALESCE(reason, ?) WHERE run_id = ?",
                (reason, run_id),
            )
            return self._fenced(conn, run_id, fence)

    def active(self) -> tuple[Record, ...]:
        with self._transaction() as conn:
            self._refresh(conn, self._clock())
            return tuple(_record(row) for row in conn.execute("SELECT * FROM runs WHERE state != 'stopped'"))

    @staticmethod
    def _refresh(conn: sqlite3.Connection, now: ClockReading) -> None:
        conn.execute(
            "UPDATE runs SET state = CASE WHEN launch_claimed = 0 AND uncertain = 0 THEN 'stopped' ELSE 'quarantined' END, "
            "stop_requested = 1, reason = CASE "
            "WHEN launch_claimed = 0 AND uncertain = 0 THEN CASE WHEN boot_id != ? THEN 'never_claimed_boot_changed' ELSE 'never_claimed_expired' END "
            "WHEN boot_id != ? THEN 'boot_changed' ELSE 'deadline_passed' END "
            "WHERE state NOT IN ('stopped', 'quarantined') AND (boot_id != ? OR deadline_boottime <= ? OR deadline_utc <= ?)",
            (now.boot_id, now.boot_id, now.boot_id, now.boottime, now.utc),
        )

    @staticmethod
    def _fenced(conn: sqlite3.Connection, run_id: str, fence: str) -> Record:
        _identity(run_id, fence)
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or not secrets.compare_digest(row["fence"], fence):
            raise StateError("stale_fence")
        return _record(row)

    @contextmanager
    def _transaction(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        conn: sqlite3.Connection | None = None
        try:
            _check_ancestors(self._path.parent)
            _check_file(self._path.lstat())
            conn = sqlite3.connect(self._path, isolation_level=None, timeout=5)
            conn.row_factory = sqlite3.Row
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise StateError("unsupported_journal_mode")
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA synchronous = EXTRA")
            conn.execute("BEGIN IMMEDIATE")
            if not initialize and conn.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise StateError("unsupported_state_version")
            try:
                yield conn
            except StateError:
                conn.rollback()
                if not initialize:
                    conn.execute("BEGIN IMMEDIATE")
                    self._refresh(conn, self._clock())
                    conn.commit()
                raise
            conn.commit()
        except sqlite3.Error:
            raise StateError("state_unavailable") from None
        finally:
            if conn is not None:
                conn.close()


def _ancestor_stat(path: Path) -> os.stat_result:
    return path.lstat()


def _check_ancestors(parent: Path, *, allow_missing: bool = False) -> None:
    for directory in (parent, *parent.parents):
        try:
            metadata = _ancestor_stat(directory)
        except FileNotFoundError:
            if allow_missing:
                continue
            raise StateError("unsafe_state_permissions") from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in (0, os.geteuid())
            or metadata.st_mode & 0o022
        ):
            raise StateError("unsafe_state_permissions")
        if directory == parent and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
            raise StateError("unsafe_state_permissions")


def _check_file(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or metadata.st_nlink != 1
    ):
        raise StateError("unsafe_state_permissions")


def _sync_directory(path: Path) -> None:
    directory = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _identity(run_id: str, fence: str) -> None:
    try:
        Request(v=1, request_id=run_id, op="status", run_id=run_id, fence=fence)
    except FrameError:
        raise StateError("invalid_identity") from None


def _uuid(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _number(value: float) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _record(row: sqlite3.Row) -> Record:
    return Record(
        row["run_id"],
        row["fence"],
        row["principal"],
        row["uid"],
        row["boot_id"],
        row["deadline_boottime"],
        row["deadline_utc"],
        row["state"],
        bool(row["launch_claimed"]),
        bool(row["stop_requested"]),
        bool(row["uncertain"]),
        row["reason"],
    )
