import array
import json
import os
import select
import socket
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from packages.agent_board.tests.night_containment_fixture import (
    Binding,
    Fault,
    GateWriter,
    Knowledge,
    SimulatedLifecycle,
    connect,
    control,
    encode,
    initialize,
    inspect,
    order,
    receive,
    run_worker,
    spawn,
    toy_value,
)

ELIGIBLE = "FIXTURE_ELIGIBLE_WITH_SIMULATED_LIFECYCLE"


@pytest.fixture
def state(tmp_path: Path) -> tuple[Path, Binding]:
    binding = Binding.new()
    directory = tmp_path / "fixture"
    initialize(directory, binding.generation)
    return directory, binding


def simulation(binding: Binding) -> SimulatedLifecycle:
    return SimulatedLifecycle(binding.generation, True, True)


def scenario(directory: Path, binding: Binding, config: dict[str, Any]) -> dict[str, Any]:
    with spawn(["driver", str(directory), json.dumps(asdict(binding)), json.dumps(config)]) as driver:
        output, errors = driver.child.communicate(timeout=20)
        assert driver.wait()["code"] == 0, errors
    return cast(dict[str, Any], json.loads(output))


def test_success_and_confirmed_domain_refusal_have_distinct_certificates(state: tuple[Path, Binding]) -> None:
    directory, binding = state
    result = scenario(directory, binding, {})
    assert result["restart"] == ELIGIBLE
    assert result["toy"] == 1 and result["report"]["certificate"] is True
    conn = connect(directory / "gate.sqlite3", readonly=True)
    try:
        record = json.loads(
            conn.execute("SELECT body FROM completions WHERE id=?", (binding.key,)).fetchone()[0]
        )
    finally:
        conn.close()
    assert record["result"]["terminal"] == "SUCCESS" and record["exit"]["code"] == 0
    assert record["result"]["entry_fds"] == sorted([0, 1, 2, record["result"]["channel_fd"]])


def test_domain_refusal_is_not_latch_success(state: tuple[Path, Binding]) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"mode": "domain"})
    assert result["restart"] == ELIGIBLE and result["toy"] == 0
    conn = connect(directory / "gate.sqlite3", readonly=True)
    try:
        record = json.loads(
            conn.execute("SELECT body FROM completions WHERE id=?", (binding.key,)).fetchone()[0]
        )
    finally:
        conn.close()
    assert record["result"]["terminal"] == "DOMAIN_REFUSED"
    assert record["result"]["primary"] == "ROLLED_BACK"


@pytest.mark.parametrize("phase", ["pending.commit", "clear.commit"])
@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("crash", [False, True])
def test_publication_knowledge_is_distinct_from_restart_state(
    state: tuple[Path, Binding], phase: str, side: str, crash: bool
) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"gate_fault": asdict(Fault(phase, side, crash))})
    assert result["control"][3] == ("PENDING" if (phase == "pending.commit") == (side == "after") else "IDLE")
    expected_idle = result["control"][3] == "IDLE"
    assert (result["restart"] == ELIGIBLE) is expected_idle
    if crash:
        assert result["report"] is None and result["exit"]["code"] == 91
    else:
        knowledge = result["report"]["pending" if phase.startswith("pending") else "clear"]
        assert knowledge == {"mutation": "UNKNOWN", "cleanup": "CLOSED"}
    if phase.startswith("pending"):
        assert result["toy"] == 0


@pytest.mark.parametrize("phase", ["pending.close", "clear.close"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_confirmed_commit_survives_cleanup_uncertainty(
    state: tuple[Path, Binding], phase: str, side: str
) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"gate_fault": asdict(Fault(phase, side))})
    knowledge = result["report"]["pending" if phase.startswith("pending") else "clear"]
    assert knowledge == {"mutation": "COMMITTED", "cleanup": "UNCERTAIN"}
    assert result["toy"] == (0 if phase.startswith("pending") else 1)
    assert (result["restart"] == ELIGIBLE) is phase.startswith("clear")


def test_real_live_old_supervisor_handle_blocks_certified_idle(state: tuple[Path, Binding]) -> None:
    directory, binding = state
    config = {"gate_fault": asdict(Fault("clear.close", "before")), "hold": True}
    with spawn(["supervisor", str(directory), json.dumps(asdict(binding)), json.dumps(config)]) as actor:
        assert actor.child.stdout is not None and actor.child.stdin is not None
        assert select.select([actor.child.stdout], [], [], 10)[0]
        report = json.loads(actor.child.stdout.readline())
        assert report["clear"] == {"mutation": "COMMITTED", "cleanup": "UNCERTAIN"}
        assert control(directory)[3] == "IDLE"
        paths = []
        for fd in Path(f"/proc/{actor.child.pid}/fd").iterdir():
            try:
                paths.append(str(fd.readlink()))
            except FileNotFoundError:
                pass
        assert str(directory / "gate.sqlite3") in paths
        assert inspect(directory, simulation(binding), (actor,)) == "OBSERVED_LIVE_OLD_ACTOR"
        actor.child.stdin.write("exit\n")
        actor.child.stdin.flush()
        assert actor.wait()["code"] == 0
        assert inspect(directory, simulation(binding), (actor,)) == ELIGIBLE


@pytest.mark.parametrize("phase", ["worker.commit", "worker.close"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_worker_faults_never_receive_normal_certificates(
    state: tuple[Path, Binding], phase: str, side: str
) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"worker_fault": asdict(Fault(phase, side))})
    assert result["report"]["certificate"] is False
    assert result["control"][3] == "PENDING" and result["restart"] != ELIGIBLE
    assert result["toy"] == (0 if (phase, side) == ("worker.commit", "before") else 1)
    knowledge = result["report"]["worker_result"]
    if phase == "worker.commit":
        assert knowledge["primary"] == "UNKNOWN" and knowledge["changed"] is None
        assert knowledge["cleanup"] == "CLOSED"
    else:
        assert knowledge["primary"] == "COMMITTED" and knowledge["cleanup"] == "UNCERTAIN"


@pytest.mark.parametrize(
    "mode",
    [
        "wrong_instance",
        "oversized",
        "partial",
        "duplicate_keys",
        "missing",
        "duplicate",
        "late_duplicate",
        "rights",
    ],
)
def test_invalid_or_duplicate_results_refuse_after_exit(state: tuple[Path, Binding], mode: str) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"mode": mode})
    assert result["report"]["certificate"] is False and result["control"][3] == "PENDING"
    assert result["restart"] != ELIGIBLE


@pytest.mark.parametrize(
    "phase,side",
    [
        ("pending.body", "before"),
        ("pending.body", "after"),
        ("clear.record", "after"),
        ("clear.body", "after"),
        ("supervisor.launch", "before"),
        ("supervisor.certificate", "before"),
    ],
)
def test_crashes_at_publication_boundaries_observe_actual_state(
    state: tuple[Path, Binding], phase: str, side: str
) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"gate_fault": asdict(Fault(phase, side, True))})
    assert result["exit"]["code"] == 91 and result["report"] is None
    assert (result["restart"] == ELIGIBLE) is phase.startswith("pending.body")
    if phase == "clear.record":
        conn = connect(directory / "gate.sqlite3", readonly=True)
        try:
            assert conn.execute("SELECT id FROM completions WHERE id=?", (binding.key,)).fetchone() is None
        finally:
            conn.close()


@pytest.mark.parametrize(
    "phase,side",
    [
        ("worker.body", "before"),
        ("worker.body", "after"),
        ("worker.commit", "before"),
        ("worker.commit", "after"),
        ("worker.report", "before"),
        ("worker.report", "after"),
    ],
)
def test_worker_crash_is_not_normal_completion(state: tuple[Path, Binding], phase: str, side: str) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"worker_fault": asdict(Fault(phase, side, True))})
    assert result["report"]["certificate"] is False and result["restart"] != ELIGIBLE


@pytest.mark.parametrize("field", ["generation", "operation", "launch"])
def test_delayed_worker_cannot_mutate_changed_owner(state: tuple[Path, Binding], field: str) -> None:
    directory, binding = state
    writer = GateWriter(directory)
    assert writer.publish(binding) == Knowledge("COMMITTED", "CLOSED")
    wrong = asdict(binding)
    wrong[field] = Binding.new().launch
    with pytest.raises(ValueError):
        run_worker(directory, Binding(**wrong), "success", Fault())
    assert toy_value(directory) == 0
    assert control(directory)[1:3] == (binding.operation, binding.launch)


def test_eof_is_not_exit_evidence(state: tuple[Path, Binding]) -> None:
    directory, binding = state
    assert GateWriter(directory).publish(binding).mutation == "COMMITTED"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with spawn(
            [
                "worker",
                str(directory),
                json.dumps(asdict(binding)),
                str(child.fileno()),
                "eof_alive",
                json.dumps(asdict(Fault())),
            ],
            pass_fds=(child.fileno(),),
        ) as actor:
            child.close()
            assert select.select([parent], [], [], 10)[0]
            assert parent.recv(4096)
            assert parent.recv(1) == b"" and not actor.exit_ready()
            assert actor.child.stdin is not None
            actor.child.stdin.write("exit\n")
            actor.child.stdin.flush()
            actor.wait()
            with pytest.raises(ValueError):
                receive(parent, binding)
    finally:
        parent.close()
        child.close()


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "bare",
        "oversized",
        "malformed",
        "wrong_generation",
        "unknown_version",
        "fault_locked",
        "wrong_bootstrap",
        "boolean_version",
    ],
)
def test_invalid_bootstrap_or_gate_never_authorizes_or_launches(
    state: tuple[Path, Binding], damage: str
) -> None:
    directory, binding = state
    conn = connect(directory / "gate.sqlite3")
    try:
        if damage == "missing":
            conn.execute("DELETE FROM completions")
        elif damage == "bare":
            conn.execute("UPDATE control SET certificate=NULL")
        elif damage == "oversized":
            conn.execute("UPDATE completions SET body=?", (b"x" * 4097,))
        elif damage == "malformed":
            conn.execute("UPDATE completions SET body=?", (b"{",))
        elif damage == "wrong_generation":
            conn.execute("UPDATE control SET generation=?", (Binding.new().generation,))
        elif damage == "unknown_version":
            conn.execute("PRAGMA user_version=2")
        elif damage == "fault_locked":
            conn.execute("UPDATE control SET state='FAULT_LOCKED'")
        else:
            record = json.loads(conn.execute("SELECT body FROM completions").fetchone()[0])
            record["simulated_authorization" if damage == "wrong_bootstrap" else "v"] = (
                "forged" if damage == "wrong_bootstrap" else True
            )
            conn.execute("UPDATE completions SET body=?", (encode(record),))
    finally:
        conn.close()
    assert inspect(directory, simulation(binding), ()) != ELIGIBLE
    assert GateWriter(directory).publish(binding).mutation == "ROLLED_BACK"
    assert toy_value(directory) == 0


def test_lifecycle_inputs_are_explicitly_simulated_and_can_refuse(state: tuple[Path, Binding]) -> None:
    directory, binding = state
    for sim in (
        SimulatedLifecycle(binding.generation, False, True),
        SimulatedLifecycle(binding.generation, True, False),
        SimulatedLifecycle(Binding.new().generation, True, True),
    ):
        assert inspect(directory, sim, ()) != ELIGIBLE
    assert inspect(directory, simulation(binding), ()) == ELIGIBLE


def test_launch_and_wait_are_outside_order_and_worker_holds_no_inherited_handles(
    state: tuple[Path, Binding],
) -> None:
    directory, binding = state
    with order(directory), pytest.raises(BlockingIOError), order(directory):
        pass
    assert scenario(directory, binding, {})["restart"] == ELIGIBLE


@pytest.mark.parametrize(
    "phase,side",
    [
        ("supervisor.launch", "after"),
        ("supervisor.exit", "after"),
        ("supervisor.result", "after"),
        ("supervisor.certificate", "after"),
        ("supervisor.reply", "before"),
        ("supervisor.reply", "after"),
    ],
)
def test_supervisor_crash_around_exit_validation_and_reply(
    state: tuple[Path, Binding], phase: str, side: str
) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"gate_fault": asdict(Fault(phase, side, True))})
    assert result["exit"]["code"] == 91
    assert (result["restart"] == ELIGIBLE) is (phase == "supervisor.reply")
    assert (result["report"] is not None) is ((phase, side) == ("supervisor.reply", "after"))


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "wrong_launch",
        "wrong_operation",
        "wrong_generation",
        "extra_field",
        "unknown_terminal",
        "wrong_terminal",
        "unknown_commit",
        "close_uncertain",
        "boolean_exit",
        "missing_exit",
        "extra_result_field",
        "duplicate_key",
        "oversized",
        "fault_locked",
    ],
)
def test_strict_normal_certificate_rejects_incomplete_or_stale_evidence(
    state: tuple[Path, Binding], damage: str
) -> None:
    directory, binding = state
    assert scenario(directory, binding, {})["restart"] == ELIGIBLE
    conn = connect(directory / "gate.sqlite3")
    try:
        record = json.loads(
            conn.execute("SELECT body FROM completions WHERE id=?", (binding.key,)).fetchone()[0]
        )
        if damage == "missing":
            conn.execute("DELETE FROM completions WHERE id=?", (binding.key,))
        elif damage == "fault_locked":
            conn.execute("UPDATE control SET state='FAULT_LOCKED'")
        else:
            if damage.startswith("wrong_") and damage != "wrong_terminal":
                record["binding"][damage.removeprefix("wrong_")] = Binding.new().launch
            elif damage == "extra_field":
                record["unexpected"] = True
            elif damage in {"unknown_terminal", "wrong_terminal"}:
                record["result"]["terminal"] = "UNKNOWN" if damage == "unknown_terminal" else "DOMAIN_REFUSED"
            elif damage == "unknown_commit":
                record["result"].update(primary="UNKNOWN", changed=None)
            elif damage == "close_uncertain":
                record["result"]["cleanup"] = "UNCERTAIN"
            elif damage == "boolean_exit":
                record["exit"]["code"] = False
            elif damage == "missing_exit":
                record.pop("exit")
            elif damage == "extra_result_field":
                record["result"]["unexpected"] = 1
            body = (
                b'{"v":1,"v":1}'
                if damage == "duplicate_key"
                else b"x" * 4097
                if damage == "oversized"
                else encode(record)
            )
            conn.execute("UPDATE completions SET body=? WHERE id=?", (body, binding.key))
    finally:
        conn.close()
    assert inspect(directory, simulation(binding), ()) != ELIGIBLE
    assert GateWriter(directory).publish(Binding.new(binding.generation)).mutation == "ROLLED_BACK"


@pytest.mark.parametrize("side", ["before", "after"])
def test_gate_rollback_failure_does_not_create_completion(state: tuple[Path, Binding], side: str) -> None:
    directory, binding = state
    writer = GateWriter(directory, Fault("pending.rollback", side))
    result = writer.publish(Binding.new(Binding.new().generation))
    assert result.mutation == "UNKNOWN" and result.cleanup == "CLOSED"
    assert control(directory)[3] == "IDLE" and toy_value(directory) == 0
    assert inspect(directory, simulation(binding), ()) == ELIGIBLE


def test_parent_can_acquire_order_while_reported_worker_waits_for_ack(state: tuple[Path, Binding]) -> None:
    directory, binding = state
    result = scenario(directory, binding, {"mode": "paused_success"})
    assert result["restart"] == ELIGIBLE and result["report"]["certificate"] is True


def test_rejected_received_descriptor_is_actually_closed(state: tuple[Path, Binding]) -> None:
    directory, binding = state
    marker = directory / "descriptor-marker"
    fd = os.open(marker, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        child.sendmsg([b"{}"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))])
        with pytest.raises(ValueError, match="ancillary"):
            receive(parent, binding)
        remaining = []
        for entry in list(Path("/proc/self/fd").iterdir()):
            try:
                if entry.readlink() == marker:
                    remaining.append(int(entry.name))
            except FileNotFoundError:
                pass
        assert remaining == [fd]
    finally:
        parent.close()
        child.close()
        os.close(fd)


def test_stale_clear_rolls_back_its_inserted_completion(state: tuple[Path, Binding]) -> None:
    directory, binding = state
    writer = GateWriter(directory)
    assert writer.publish(binding) == Knowledge("COMMITTED", "CLOSED")
    certificate = run_worker(directory, binding, "success", Fault())
    replacement = Binding.new(binding.generation)
    with order(directory):
        conn = connect(directory / "gate.sqlite3")
        try:
            conn.execute(
                "UPDATE control SET operation=?,launch=? WHERE id=1",
                (replacement.operation, replacement.launch),
            )
        finally:
            conn.close()
    assert writer.publish(binding, certificate) == Knowledge("ROLLED_BACK", "CLOSED")
    assert control(directory)[1:4] == (replacement.operation, replacement.launch, "PENDING")
    conn = connect(directory / "gate.sqlite3", readonly=True)
    try:
        assert conn.execute("SELECT id FROM completions WHERE id=?", (binding.key,)).fetchone() is None
    finally:
        conn.close()
