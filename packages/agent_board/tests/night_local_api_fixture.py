import array
import json
import os
import select
import socket
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import uuid4

from agent_board.night import journal_latch as api
from agent_board.night.emission_lock import EmissionLock
from agent_board.night_state import Grant, LocalJournal, check_private_file, clock_reading

if TYPE_CHECKING:
    from packages.agent_board.tests.night_containment_fixture import OwnedChild, decode, encode
else:
    from night_containment_fixture import OwnedChild, decode, encode

CAP = 4096


@dataclass(frozen=True)
class Case:
    directory: Path
    run_id: str
    fence: str

    @property
    def journal(self) -> Path:
        return self.directory / "journal.sqlite3"

    @property
    def lock(self) -> Path:
        return self.directory / "order"


@dataclass(frozen=True)
class SQLFault:
    operation: str
    ordinal: int
    side: str


@dataclass(frozen=True)
class Observation:
    reported: dict[str, Any]
    current: tuple[Any, ...]
    exited: dict[str, Any]
    probed_while_alive: bool


def setup(directory: Path, *, expired: bool = False) -> Case:
    if os.geteuid() == 0:
        raise ValueError("requires_unprivileged_fixture")
    case = Case(directory, str(uuid4()), "f" * 43)
    reading = clock_reading()
    grant = Grant(
        case.run_id,
        case.fence,
        "fixture@dev",
        2001,
        reading.boot_id,
        reading.boottime + 600,
        reading.utc + 600,
    )
    journal = LocalJournal(case.journal, box="dev", principals={"fixture@dev": 2001})
    journal.reserve(grant)
    journal.claim_start(case.run_id, case.fence)
    fd = os.open(case.lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    os.close(fd)
    check_private_file(case.journal)
    check_private_file(case.lock)
    if expired:
        conn = sqlite3.connect(case.journal, isolation_level=None)
        try:
            conn.execute("UPDATE runs SET deadline_boottime=0,deadline_utc=0 WHERE run_id=?", (case.run_id,))
        finally:
            conn.close()
    return case


def row(case: Case) -> tuple[Any, ...]:
    conn = sqlite3.connect(f"{case.journal.as_uri()}?mode=ro", uri=True, timeout=0, isolation_level=None)
    try:
        found = conn.execute(
            "SELECT state,launch_claimed,stop_requested,uncertain,reason FROM runs WHERE run_id=?",
            (case.run_id,),
        ).fetchone()
        if found is None:
            raise ValueError("missing_fixture_row")
        return tuple(found)
    finally:
        conn.close()


def fd_inventory() -> dict[int, str]:
    result: dict[int, str] = {}
    for entry in list(Path("/proc/self/fd").iterdir()):
        try:
            result[int(entry.name)] = str(entry.readlink())
        except FileNotFoundError:
            pass
    return result


class FaultConnection(sqlite3.Connection):
    faults: tuple[SQLFault, ...]
    counts: dict[str, int]
    statements: list[str]

    def hit(self, operation: str, ordinal: int, side: str) -> None:
        if SQLFault(operation, ordinal, side) in self.faults:
            raise sqlite3.OperationalError("compatibility_fixture_fault")

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        operation = sql.split()[0].upper()
        ordinal = self.counts.get(operation, 0) + 1
        self.counts[operation] = ordinal
        self.statements.append(sql)
        self.hit(operation, ordinal, "before")
        cursor = super().execute(sql, parameters)
        self.hit(operation, ordinal, "after")
        return cursor

    def close(self) -> None:
        ordinal = self.counts.get("CLOSE", 0) + 1
        self.counts["CLOSE"] = ordinal
        self.hit("CLOSE", ordinal, "before")
        super().close()
        self.hit("CLOSE", ordinal, "after")


def result_data(found: api.LatchResult) -> dict[str, Any]:
    return {
        "primary": found.primary.name,
        "expiry": found.expiry.name,
        "cleanup": found.cleanup.name,
        "primary_changed": found.primary_changed,
        "latch_changed": found.latch_changed,
        "expiry_changed": found.expiry_changed,
        "record_state": found.record.state if found.record else None,
        "domain": found.domain,
        "fault": found.fault,
    }


def worker(case: Case, channel: socket.socket, mode: str, faults: tuple[SQLFault, ...]) -> None:
    entry = fd_inventory()
    if sorted(entry) != sorted([0, 1, 2, channel.fileno()]):
        raise ValueError("unexpected_entry_descriptors")
    lock = EmissionLock(case.lock)
    opened: list[FaultConnection] = []
    original = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if kwargs.get("timeout") != 0 or kwargs.get("uri") is not True or "mode=rw" not in str(args[0]):
            raise ValueError("unexpected_critical_connection_policy")
        conn = original(*args, **kwargs, factory=FaultConnection)
        conn.faults, conn.counts, conn.statements = faults, {}, []
        opened.append(conn)
        return conn

    with patch("sqlite3.connect", side_effect=connect), lock.held():
        found = api.latch(case.journal, run_id=case.run_id, fence="e" * 43 if mode == "stale" else case.fence)
    after = fd_inventory()
    report = {
        "kind": "LOCAL_API_COMPATIBILITY_V1",
        "run_id": case.run_id,
        "entry_fds": sorted(entry),
        "channel_fd": channel.fileno(),
        "result": result_data(found),
        "journal_fds_after_api": sorted(
            fd for fd, target in after.items() if target.startswith(str(case.journal))
        ),
        "coordinator_fds_after_api": sorted(fd for fd, target in after.items() if target == str(case.lock)),
        "sql_operations": [statement.split()[0].upper() for conn in opened for statement in conn.statements],
    }
    data = encode(report)
    if len(data) > CAP:
        raise ValueError("oversized_fixture_result")
    channel.send(data, socket.MSG_DONTWAIT)
    if mode == "pause":
        channel.recv(1)


def receive(channel: socket.socket, case: Case) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    for ordinal in range(2):
        try:
            data, ancillary, flags, _ = channel.recvmsg(CAP, socket.CMSG_SPACE(64), socket.MSG_DONTWAIT)
        except BlockingIOError:
            if result is None:
                raise ValueError("missing_result") from None
            return result
        for level, kind, raw in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds = array.array("i")
                fds.frombytes(raw[: len(raw) - len(raw) % fds.itemsize])
                for fd in fds:
                    os.close(fd)
        if ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or not data or ordinal != 0:
            raise ValueError("invalid_exchange")
        result = decode(data)
        validate(result, case)
    raise ValueError("extra_record")


def validate(value: dict[str, Any], case: Case) -> None:
    if set(value) != {
        "kind",
        "run_id",
        "entry_fds",
        "channel_fd",
        "result",
        "journal_fds_after_api",
        "coordinator_fds_after_api",
        "sql_operations",
    }:
        raise ValueError("report_shape")
    if value["kind"] != "LOCAL_API_COMPATIBILITY_V1" or value["run_id"] != case.run_id:
        raise ValueError("report_binding")
    fd = value["channel_fd"]
    if type(fd) is not int or fd <= 2 or value["entry_fds"] != sorted([0, 1, 2, fd]):
        raise ValueError("entry_inventory")
    for field in ("entry_fds", "journal_fds_after_api", "coordinator_fds_after_api"):
        if not isinstance(value[field], list) or any(
            type(item) is not int or item < 0 for item in value[field]
        ):
            raise ValueError("fd_inventory")
    found = value["result"]
    if not isinstance(found, dict):
        raise TypeError("result_not_object")
    if set(found) != {
        "primary",
        "expiry",
        "cleanup",
        "primary_changed",
        "latch_changed",
        "expiry_changed",
        "record_state",
        "domain",
        "fault",
    }:
        raise ValueError("result_shape")
    for name in ("primary", "expiry"):
        if type(found[name]) is not str or found[name] not in api.MutationOutcome.__members__:
            raise ValueError("mutation_enum")
    if type(found["cleanup"]) is not str or found["cleanup"] not in api.CleanupOutcome.__members__:
        raise ValueError("cleanup_enum")
    for name in ("primary_changed", "latch_changed", "expiry_changed"):
        if found[name] is not None and type(found[name]) is not bool:
            raise ValueError("changed_type")
    for name in ("record_state", "domain", "fault"):
        if found[name] is not None and (type(found[name]) is not str or len(found[name]) > 256):
            raise ValueError("result_text")
    enums = {
        "record_state": {None, "reserved", "starting", "stopping", "quarantined", "stopped"},
        "domain": {None, "stale_fence"},
        "fault": {
            None,
            "invalid_selector",
            "validation_or_path_failure",
            "sqlite_failure",
            "rollback_failure",
            "close_failure",
        },
    }
    if any(found[name] not in allowed for name, allowed in enums.items()):
        raise ValueError("result_text_enum")
    if not isinstance(value["sql_operations"], list) or len(value["sql_operations"]) > 21:
        raise ValueError("operation_count")
    if any(
        type(op) is not str or op not in {"PRAGMA", "BEGIN", "SELECT", "UPDATE", "COMMIT", "ROLLBACK"}
        for op in value["sql_operations"]
    ):
        raise ValueError("operation_enum")


def run(case: Case, *, mode: str = "success", faults: tuple[SQLFault, ...] = ()) -> Observation:
    receiver, sender_guard = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                str(case.directory),
                case.run_id,
                case.fence,
                str(sender_guard.fileno()),
                mode,
                json.dumps([asdict(fault) for fault in faults]),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
            pass_fds=(sender_guard.fileno(),),
        )
        with OwnedChild(child) as owned:
            probed = False
            if mode == "pause":
                if not select.select([receiver], [], [], 10)[0] or owned.exit_ready():
                    raise ValueError("no_live_reporter")
                with EmissionLock(case.lock).held():
                    probed = True
                receiver.send(b"a", socket.MSG_DONTWAIT)
            exited = owned.wait()
            if exited["code"] != 0:
                assert child.stderr is not None
                raise ValueError(child.stderr.read())
            report = receive(receiver, case)
        return Observation(report, row(case), exited, probed)
    finally:
        receiver.close()
        sender_guard.close()


def main() -> None:
    directory, run_id, fence, raw_fd, mode, raw_faults = sys.argv[1:]
    if os.geteuid() == 0:
        raise ValueError("requires_unprivileged_fixture")
    case = Case(Path(directory), run_id, fence)
    with socket.socket(fileno=int(raw_fd)) as channel:
        worker(case, channel, mode, tuple(SQLFault(**item) for item in json.loads(raw_faults)))


if __name__ == "__main__":
    main()
