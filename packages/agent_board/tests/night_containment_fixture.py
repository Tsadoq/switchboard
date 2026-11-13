import array
import ctypes
import fcntl
import json
import os
import select
import signal
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

CAP = 4096
ZERO = str(UUID(int=0))


@dataclass(frozen=True)
class Binding:
    generation: str
    operation: str
    launch: str

    @classmethod
    def new(cls, generation: str | None = None) -> "Binding":
        return cls(generation or str(uuid4()), str(uuid4()), str(uuid4()))

    @property
    def key(self) -> str:
        return ":".join(asdict(self).values())


@dataclass(frozen=True)
class SimulatedLifecycle:
    expected_generation: str
    maintenance_clear: bool
    installed_participants_fenced: bool


@dataclass(frozen=True)
class Fault:
    phase: str = ""
    side: str = ""
    crash: bool = False

    def hit(self, phase: str, side: str) -> None:
        if (phase, side) == (self.phase, self.side):
            if self.crash:
                os._exit(91)
            raise sqlite3.OperationalError("fixture_injected_fault")


@dataclass(frozen=True)
class Knowledge:
    mutation: str
    cleanup: str


class ResultRefusal(ValueError):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("uncertifiable_worker_result")
        self.result = result


class OwnedChild:
    def __init__(self, child: subprocess.Popen[str]) -> None:
        self.child = child
        try:
            self.pidfd = os.pidfd_open(child.pid)
        except OSError:
            child.kill()
            child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
            raise
        self.closed = False

    def exit_ready(self) -> bool:
        if self.closed:
            raise ValueError("closed_lifetime_witness")
        poll = select.poll()
        poll.register(self.pidfd, select.POLLIN)
        return bool(poll.poll(0))

    def wait(self) -> dict[str, Any]:
        code = self.child.wait(timeout=10)
        if not self.exit_ready():
            raise ValueError("missing_kernel_exit")
        return {"pid": self.child.pid, "code": code, "kind": "owned_child_waitpid"}

    def close(self) -> None:
        if not self.closed:
            if not self.exit_ready():
                signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
            self.child.wait(timeout=10)
            os.close(self.pidfd)
            for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
                if stream is not None:
                    stream.close()
            self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def encode(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def decode(data: bytes) -> dict[str, Any]:
    if len(data) > CAP:
        raise ValueError("oversized")
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise TypeError("not_object")
    return value


def binding_from(value: Any) -> Binding:
    if not isinstance(value, dict) or set(value) != {"generation", "operation", "launch"}:
        raise ValueError("binding_shape")
    for text in value.values():
        if not isinstance(text, str) or str(UUID(text)) != text:
            raise ValueError("binding_uuid")
    return Binding(**value)


def validate_result(value: dict[str, Any], binding: Binding) -> None:
    if set(value) != {
        "binding",
        "primary",
        "cleanup",
        "terminal",
        "changed",
        "fault",
        "entry_fds",
        "channel_fd",
    }:
        raise ValueError("result_shape")
    if binding_from(value["binding"]) != binding:
        raise ValueError("wrong_instance")
    if not isinstance(value["primary"], str) or value["primary"] not in {
        "COMMITTED",
        "ROLLED_BACK",
        "UNKNOWN",
        "NOT_ATTEMPTED",
    }:
        raise ValueError("mutation_enum")
    if not isinstance(value["cleanup"], str) or value["cleanup"] not in {"CLOSED", "UNCERTAIN"}:
        raise ValueError("cleanup_enum")
    if value["terminal"] is not None and (
        not isinstance(value["terminal"], str) or value["terminal"] not in {"SUCCESS", "DOMAIN_REFUSED"}
    ):
        raise ValueError("terminal_enum")
    if value["changed"] is not None and type(value["changed"]) is not bool:
        raise ValueError("changed_type")
    if value["fault"] is not None and (
        not isinstance(value["fault"], str) or value["fault"] not in {"INJECTED", "OWNERSHIP_REFUSED"}
    ):
        raise ValueError("fault_enum")
    if value["primary"] == "UNKNOWN" and value["changed"] is not None:
        raise ValueError("unknown_change")
    fd = value["channel_fd"]
    if type(fd) is not int or fd <= 2 or not isinstance(value["entry_fds"], list):
        raise ValueError("descriptor_shape")
    if any(type(item) is not int for item in value["entry_fds"]) or value["entry_fds"] != sorted(
        [0, 1, 2, fd]
    ):
        raise ValueError("descriptor_whitelist")


@contextmanager
def order(directory: Path) -> Iterator[None]:
    fd = os.open(directory / "order", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    mode = "ro" if readonly else "rw"
    conn = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=0")
    return conn


def initialize(directory: Path, generation: str) -> None:
    directory.mkdir(mode=0o700, parents=True)
    (directory / "order").touch(mode=0o600)
    binding = Binding(generation, ZERO, ZERO)
    bootstrap = {
        "v": 1,
        "kind": "BOOTSTRAP",
        "binding": asdict(binding),
        "simulated_authorization": "temporary_fixture_only",
    }
    conn = sqlite3.connect(directory / "gate.sqlite3", isolation_level=None)
    try:
        conn.executescript(
            "PRAGMA user_version=1;"
            "CREATE TABLE control(id INTEGER PRIMARY KEY CHECK(id=1),generation TEXT NOT NULL,"
            "operation TEXT NOT NULL,launch TEXT NOT NULL,state TEXT NOT NULL,certificate TEXT);"
            "CREATE TABLE completions(id TEXT PRIMARY KEY,body BLOB NOT NULL);"
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO completions VALUES(?,?)", (binding.key, encode(bootstrap)))
        conn.execute("INSERT INTO control VALUES(1,?,?,?,'IDLE',?)", (*asdict(binding).values(), binding.key))
        conn.execute("COMMIT")
    finally:
        conn.close()
    conn = sqlite3.connect(directory / "toy.sqlite3", isolation_level=None)
    try:
        conn.execute("CREATE TABLE toy(id INTEGER PRIMARY KEY CHECK(id=1),latched INTEGER NOT NULL)")
        conn.execute("INSERT INTO toy VALUES(1,0)")
    finally:
        conn.close()


def control(directory: Path) -> tuple[Any, ...]:
    conn = connect(directory / "gate.sqlite3", readonly=True)
    try:
        if conn.execute("PRAGMA user_version").fetchone() != (1,):
            raise ValueError("gate_version")
        row = conn.execute(
            "SELECT generation,operation,launch,state,certificate FROM control WHERE id=1"
        ).fetchone()
        if row is None:
            raise ValueError("missing_control")
        return tuple(row)
    finally:
        conn.close()


def idle_evidence(conn: sqlite3.Connection, directory: Path, expected_generation: str) -> None:
    if conn.execute("PRAGMA user_version").fetchone() != (1,):
        raise ValueError("gate_version")
    row = conn.execute(
        "SELECT generation,operation,launch,state,certificate FROM control WHERE id=1"
    ).fetchone()
    if row is None:
        raise ValueError("missing_control")
    generation, operation, launch, state, reference = row
    binding = binding_from({"generation": generation, "operation": operation, "launch": launch})
    if generation != expected_generation or state != "IDLE" or reference != binding.key:
        raise ValueError("gate_refusal")
    record = conn.execute(
        "SELECT body FROM completions WHERE id=? AND typeof(body)='blob' AND length(body)<=?",
        (reference, CAP),
    ).fetchone()
    if record is None:
        raise ValueError("missing_certificate")
    certificate = decode(record[0])
    validate_certificate(certificate, binding)
    if certificate["kind"] == "NORMAL" and certificate["toy_latched"] != toy_value(directory):
        raise ValueError("current_state_mismatch")


class GateWriter:
    def __init__(self, directory: Path, fault: Fault | None = None) -> None:
        self.directory = directory
        self.fault = fault or Fault()
        self.retained: list[sqlite3.Connection] = []

    def publish(self, binding: Binding, certificate: dict[str, Any] | None = None) -> Knowledge:
        phase = "clear" if certificate is not None else "pending"
        attempted = False
        outcome = "NOT_ATTEMPTED"
        cleanup = "CLOSED"
        with order(self.directory):
            conn = connect(self.directory / "gate.sqlite3")
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.fault.hit(f"{phase}.body", "before")
                if certificate is None:
                    idle_evidence(conn, self.directory, binding.generation)
                    if binding.operation == ZERO or binding.launch == ZERO:
                        raise ValueError("bootstrap_not_operation")
                    changed = conn.execute(
                        "UPDATE control SET operation=?,launch=?,state='PENDING',certificate=NULL "
                        "WHERE id=1 AND generation=? AND state='IDLE'",
                        (binding.operation, binding.launch, binding.generation),
                    ).rowcount
                else:
                    validate_certificate(certificate, binding)
                    if certificate["kind"] != "NORMAL":
                        raise ValueError("clear_requires_normal_completion")
                    conn.execute("INSERT INTO completions VALUES(?,?)", (binding.key, encode(certificate)))
                    self.fault.hit("clear.record", "after")
                    changed = conn.execute(
                        "UPDATE control SET state='IDLE',certificate=? WHERE id=1 AND generation=? "
                        "AND operation=? AND launch=? AND state='PENDING'",
                        (binding.key, *asdict(binding).values()),
                    ).rowcount
                if changed != 1:
                    raise ValueError("gate_owner_refused")
                self.fault.hit(f"{phase}.body", "after")
                attempted = True
                self.fault.hit(f"{phase}.commit", "before")
                conn.execute("COMMIT")
                self.fault.hit(f"{phase}.commit", "after")
                outcome = "COMMITTED"
            except (sqlite3.Error, ValueError, TypeError):
                outcome = "UNKNOWN" if attempted else "NOT_ATTEMPTED"
                try:
                    self.fault.hit(f"{phase}.rollback", "before")
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    self.fault.hit(f"{phase}.rollback", "after")
                    if not attempted:
                        outcome = "ROLLED_BACK"
                except sqlite3.Error:
                    outcome = "UNKNOWN"
            finally:
                try:
                    self.fault.hit(f"{phase}.close", "before")
                    conn.close()
                    self.fault.hit(f"{phase}.close", "after")
                except sqlite3.Error:
                    cleanup = "UNCERTAIN"
                    self.retained.append(conn)
        return Knowledge(outcome, cleanup)

    def close(self) -> None:
        for conn in self.retained:
            conn.close()
        self.retained.clear()


def toy_value(directory: Path) -> int:
    conn = connect(directory / "toy.sqlite3", readonly=True)
    try:
        value = conn.execute("SELECT latched FROM toy WHERE id=1").fetchone()
        if value is None or type(value[0]) is not int or value[0] not in (0, 1):
            raise ValueError("toy_row")
        return int(value[0])
    finally:
        conn.close()


def validate_certificate(value: dict[str, Any], binding: Binding) -> None:
    if (
        value.get("v") != 1
        or type(value.get("v")) is not int
        or binding_from(value.get("binding")) != binding
    ):
        raise ValueError("certificate_binding")
    if value.get("kind") == "BOOTSTRAP":
        if set(value) != {"v", "kind", "binding", "simulated_authorization"}:
            raise ValueError("bootstrap_shape")
        if binding.operation != ZERO or binding.launch != ZERO:
            raise ValueError("bootstrap_instance")
        if value["simulated_authorization"] != "temporary_fixture_only":
            raise ValueError("bootstrap_authorization")
        return
    if set(value) != {"v", "kind", "binding", "result", "exit", "toy_latched"} or value["kind"] != "NORMAL":
        raise ValueError("certificate_shape")
    if not isinstance(value["result"], dict):
        raise TypeError("certificate_result")
    result = value["result"]
    validate_result(result, binding)
    if result["cleanup"] != "CLOSED" or result["fault"] is not None:
        raise ValueError("uncertifiable_cleanup_fault")
    expected = {"SUCCESS": ("COMMITTED", True, 1), "DOMAIN_REFUSED": ("ROLLED_BACK", False, 0)}
    if result["terminal"] not in expected:
        raise ValueError("uncertifiable_terminal")
    primary, changed, latched = expected[result["terminal"]]
    if result["primary"] != primary or result["changed"] is not changed:
        raise ValueError("uncertifiable_mutation")
    if type(value["toy_latched"]) is not int or value["toy_latched"] != latched:
        raise ValueError("certificate_observation")
    exited = value["exit"]
    if not isinstance(exited, dict) or set(exited) != {"pid", "code", "kind"}:
        raise ValueError("exit_shape")
    if type(exited["pid"]) is not int or exited["pid"] <= 0:
        raise ValueError("exit_pid")
    if type(exited["code"]) is not int or exited["code"] != 0 or exited["kind"] != "owned_child_waitpid":
        raise ValueError("exit_status")


def inspect(directory: Path, simulation: SimulatedLifecycle, previous: tuple[OwnedChild, ...]) -> str:
    if not simulation.maintenance_clear or not simulation.installed_participants_fenced:
        return "SIMULATED_LIFECYCLE_REFUSAL"
    if any(not child.exit_ready() for child in previous):
        return "OBSERVED_LIVE_OLD_ACTOR"
    try:
        with order(directory):
            conn = connect(directory / "gate.sqlite3", readonly=True)
            try:
                idle_evidence(conn, directory, simulation.expected_generation)
            finally:
                conn.close()
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return "INVALID_STATE_REFUSAL"
    return "FIXTURE_ELIGIBLE_WITH_SIMULATED_LIFECYCLE"


def worker(directory: Path, binding: Binding, channel: socket.socket, mode: str, fault: Fault) -> None:
    entry_fds: list[int] = []
    for path in list(Path("/proc/self/fd").iterdir()):
        try:
            path.readlink()
        except FileNotFoundError:
            continue
        entry_fds.append(int(path.name))
    result: dict[str, Any] = {
        "binding": asdict(binding),
        "primary": "NOT_ATTEMPTED",
        "cleanup": "CLOSED",
        "terminal": None,
        "changed": False,
        "fault": None,
        "entry_fds": sorted(entry_fds),
        "channel_fd": channel.fileno(),
    }
    validate_result(result, binding)
    retained: sqlite3.Connection | None = None
    conn: sqlite3.Connection | None = None
    attempted = False
    with order(directory):
        if control(directory)[:4] != (*asdict(binding).values(), "PENDING"):
            result["fault"] = "OWNERSHIP_REFUSED"
        else:
            conn = connect(directory / "toy.sqlite3")
            try:
                conn.execute("BEGIN IMMEDIATE")
                fault.hit("worker.body", "before")
                if mode == "domain":
                    conn.execute("ROLLBACK")
                    result.update(primary="ROLLED_BACK", terminal="DOMAIN_REFUSED")
                else:
                    conn.execute("UPDATE toy SET latched=1 WHERE id=1")
                    fault.hit("worker.body", "after")
                    attempted = True
                    fault.hit("worker.commit", "before")
                    conn.execute("COMMIT")
                    fault.hit("worker.commit", "after")
                    result.update(primary="COMMITTED", terminal="SUCCESS", changed=True)
            except sqlite3.Error:
                result.update(
                    primary="UNKNOWN" if attempted else "ROLLED_BACK",
                    changed=None if attempted else False,
                    fault="INJECTED",
                )
                try:
                    fault.hit("worker.rollback", "before")
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    fault.hit("worker.rollback", "after")
                except sqlite3.Error:
                    result.update(primary="UNKNOWN", changed=None)
            finally:
                try:
                    fault.hit("worker.close", "before")
                    conn.close()
                    fault.hit("worker.close", "after")
                except sqlite3.Error:
                    result["cleanup"] = "UNCERTAIN"
                    retained = conn
    fault.hit("worker.report", "before")
    data = encode(result)
    if mode == "wrong_instance":
        result["binding"] = asdict(Binding.new(binding.generation))
        data = encode(result)
    if mode == "oversized":
        data = b"x" * (CAP + 1)
    if mode == "partial":
        data = data[:-1]
    if mode == "duplicate_keys":
        data = b'{"primary":1,"primary":2}'
    if mode != "missing":
        ancillary = []
        if mode == "rights":
            ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [channel.fileno()]))]
        channel.sendmsg([data], ancillary, socket.MSG_DONTWAIT)
        if mode in {"late_duplicate", "paused_success"}:
            channel.recv(1)
        if mode in {"duplicate", "late_duplicate"}:
            channel.send(data, socket.MSG_DONTWAIT)
    fault.hit("worker.report", "after")
    if mode == "eof_alive":
        channel.close()
        sys.stdin.readline()
    if retained is not None:
        retained = None


def receive(channel: socket.socket, binding: Binding) -> dict[str, Any]:
    packets: list[bytes] = []
    channel.setblocking(False)
    for _ in range(2):
        try:
            data, anc, flags, _ = channel.recvmsg(CAP, socket.CMSG_SPACE(16 * array.array("i").itemsize))
        except BlockingIOError:
            break
        unexpected = bool(anc)
        for level, kind, raw in anc:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds = array.array("i")
                fds.frombytes(raw[: len(raw) - len(raw) % fds.itemsize])
                for fd in fds:
                    os.close(fd)
        if unexpected or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError("result_ancillary_or_truncation")
        if not data:
            break
        packets.append(data)
    if len(packets) != 1:
        raise ValueError("result_count")
    result = decode(packets[0])
    validate_result(result, binding)
    return result


def spawn(arguments: list[str], *, pass_fds: tuple[int, ...] = ()) -> OwnedChild:
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
        pass_fds=pass_fds,
    )
    return OwnedChild(child)


def run_worker(
    directory: Path, binding: Binding, mode: str, fault: Fault, supervisor_fault: Fault | None = None
) -> dict[str, Any]:
    supervisor_fault = supervisor_fault or Fault()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with spawn(
            [
                "worker",
                str(directory),
                json.dumps(asdict(binding)),
                str(child.fileno()),
                mode,
                json.dumps(asdict(fault)),
            ],
            pass_fds=(child.fileno(),),
        ) as owned:
            child.close()
            supervisor_fault.hit("supervisor.launch", "after")
            if mode in {"late_duplicate", "paused_success"}:
                ready = select.select([parent], [], [], 10)[0]
                if not ready:
                    raise ValueError("missing_first_packet")
                with order(directory):
                    pass
                parent.send(b"a", socket.MSG_DONTWAIT)
            exited = owned.wait()
            supervisor_fault.hit("supervisor.exit", "after")
            result = receive(parent, binding)
            supervisor_fault.hit("supervisor.result", "after")
            certificate = {
                "v": 1,
                "kind": "NORMAL",
                "binding": asdict(binding),
                "result": result,
                "exit": exited,
                "toy_latched": toy_value(directory),
            }
            try:
                validate_certificate(certificate, binding)
            except ValueError:
                raise ResultRefusal(result) from None
            supervisor_fault.hit("supervisor.certificate", "before")
            return certificate
    finally:
        parent.close()
        child.close()


def supervisor(directory: Path, binding: Binding, config: dict[str, Any]) -> None:
    fault = Fault(**config.get("gate_fault", {}))
    writer = GateWriter(directory, fault)
    pending = writer.publish(binding)
    report: dict[str, Any] = {"pending": asdict(pending), "clear": None, "certificate": False}
    if pending == Knowledge("COMMITTED", "CLOSED"):
        fault.hit("supervisor.launch", "before")
        try:
            certificate = run_worker(
                directory,
                binding,
                config.get("mode", "success"),
                Fault(**config.get("worker_fault", {})),
                fault,
            )
            fault.hit("supervisor.certificate", "after")
            report["clear"] = asdict(writer.publish(binding, certificate))
            report["certificate"] = True
            report["worker_result"] = certificate["result"]
        except ResultRefusal as refused:
            report["worker_refused"] = True
            report["worker_result"] = refused.result
        except (ValueError, TypeError, OSError, subprocess.TimeoutExpired):
            report["worker_refused"] = True
    fault.hit("supervisor.reply", "before")
    print(json.dumps(report), flush=True)
    fault.hit("supervisor.reply", "after")
    if config.get("hold"):
        sys.stdin.readline()
    writer.close()


def _subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "fixture_subreaper")


def _cleanup_owned_descendants() -> None:
    path = Path(f"/proc/self/task/{os.getpid()}/children")
    while children := path.read_text().split():
        for text in children:
            pid = int(text)
            fd = os.pidfd_open(pid)
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(fd)
            os.waitpid(pid, 0)


def driver(directory: Path, binding: Binding, config: dict[str, Any]) -> None:
    _subreaper()
    try:
        with spawn(["supervisor", str(directory), json.dumps(asdict(binding)), json.dumps(config)]) as actor:
            output, errors = actor.child.communicate(timeout=15)
            exited = actor.wait()
            _cleanup_owned_descendants()
            simulation = SimulatedLifecycle(binding.generation, True, True)
            restart = inspect(directory, simulation, (actor,))
        print(
            json.dumps(
                {
                    "report": json.loads(output) if output else None,
                    "exit": exited,
                    "errors": errors,
                    "restart": restart,
                    "control": control(directory),
                    "toy": toy_value(directory),
                }
            ),
            flush=True,
        )
    finally:
        _cleanup_owned_descendants()


def main() -> None:
    mode, raw_directory, raw_binding, *rest = sys.argv[1:]
    directory = Path(raw_directory)
    binding = binding_from(json.loads(raw_binding))
    if mode == "worker":
        fd, behavior, raw_fault = rest
        with socket.socket(fileno=int(fd)) as channel:
            worker(directory, binding, channel, behavior, Fault(**json.loads(raw_fault)))
    elif mode == "supervisor":
        supervisor(directory, binding, json.loads(rest[0]))
    elif mode == "driver":
        driver(directory, binding, json.loads(rest[0]))
    else:
        raise ValueError("fixture_mode")


if __name__ == "__main__":
    main()
