import json
import os
import socket
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board.night import server
from agent_board.night.config import VerifierConfigError, parse
from agent_board.night.controller import Controller, Observation, RunSpec, ServerGrant, unit_name
from agent_board.runner import night_state
from agent_board.runner.night_protocol import Operation, Request
from agent_board.runner.night_state import ClockReading, LocalJournal

PI_UID, CLAUDE_UID, STRANGER_UID = 2001, 2002, 2999
FENCE = "f" * 43


def _config_table(**changes: Any) -> dict[str, Any]:
    table: dict[str, Any] = {
        "box": "dev",
        "principal": "verifier@dev",
        "board_url": "https://board.test.invalid",
        "namespace": "agent-board@board.test.invalid",
        "signer_key": "/etc/switchboard-night/verifier_ed25519",
        "state_path": "/var/lib/switchboard-night/journal.sqlite3",
        "slice": "switchboard-night.slice",
        "launcher": {"python": "/opt/switchboard-night/bin/python", "module": "agent_board.night.launch"},
        "agents": {
            "pi@dev": {
                "uid": PI_UID,
                "home": "/home/pi",
                "happier_home": "/var/lib/switchboard-night/happier/pi",
                "workdir_root": "/srv/night/pi",
            },
            "claude@dev": {
                "uid": CLAUDE_UID,
                "home": "/home/claude",
                "happier_home": "/var/lib/switchboard-night/happier/claude",
                "workdir_root": "/srv/night/claude",
            },
        },
        "limits": {"memory_max": "600M", "tasks_max": 128, "cpu_quota": "100%", "stop_grace_seconds": 5},
    }
    table.update(changes)
    return table


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        pytest.param({"box": "ops"}, "excluded", id="excluded-box"),
        pytest.param({"principal": "claude@dev"}, "verifier@<box>", id="non-verifier-principal"),
        pytest.param({"board_url": "http://board.test.invalid"}, "https", id="plain-http"),
        pytest.param({"extra": 1}, "unknown keys", id="unknown-key"),
        pytest.param({"state_path": "/var/lib/../tmp/journal"}, "normalized", id="dotdot-path"),
        pytest.param({"state_path": "relative/journal"}, "normalized", id="relative-path"),
        pytest.param(
            {"launcher": {"python": "python3", "module": "x"}}, "normalized", id="path-lookup-python"
        ),
        pytest.param({"slice": "user.slice/../x"}, "slice", id="bad-slice"),
    ],
)
def test_config_refuses_unsafe_values(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(VerifierConfigError, match=message):
        parse(_config_table(**changes))


def test_config_refuses_agents_from_another_box() -> None:
    agents = _config_table()["agents"]

    with pytest.raises(VerifierConfigError, match="does not belong"):
        parse(_config_table(agents={**agents, "pi@vault": agents["pi@dev"]}))


def test_kinds_sharing_one_os_account_map_to_both_principals() -> None:
    agents = _config_table()["agents"]
    shared = {"pi@dev": agents["pi@dev"], "claude@dev": {**agents["claude@dev"], "uid": PI_UID}}

    config = parse(_config_table(agents=shared))

    assert config.principals_for_uid(PI_UID) == {"pi@dev", "claude@dev"}
    assert config.principals_for_uid(STRANGER_UID) == frozenset()


class Clock:
    def __init__(self) -> None:
        self.value = ClockReading(str(uuid4()), 100.0, 1_000.0)
        self.monotonic = 0.0

    def __call__(self) -> ClockReading:
        return self.value


class Grants:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.grant = ServerGrant("pi@dev", "dev", "admitted", 900.0, 1_000.0 + 3_600.0)
        self.failure: Exception | None = None
        self.rtt = 0.0
        self.calls = 0

    def fetch_grant(self, run_id: str, fence: str) -> ServerGrant:
        self.calls += 1
        self.clock.monotonic += self.rtt
        if self.failure is not None:
            raise self.failure
        return self.grant


class Runs:
    def __init__(self) -> None:
        self.launched: list[RunSpec] = []
        self.stopped: list[str] = []
        self.observation = Observation(unit_active=False, cgroup_populated=False)
        self.launch_failure: Exception | None = None

    def launch(self, spec: RunSpec) -> None:
        if self.launch_failure is not None:
            raise self.launch_failure
        self.launched.append(spec)

    def stop(self, unit: str) -> None:
        self.stopped.append(unit)

    def inspect(self, unit: str) -> Observation:
        return self.observation


@pytest.fixture(autouse=True)
def protected_fixture_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def metadata(path: Path) -> os.stat_result:
        result = path.lstat()
        if path in tmp_path.parents:
            values = list(result)
            values[0] = 0o40755
            values[4] = 0
            return os.stat_result(values)
        return result

    monkeypatch.setattr(night_state, "_ancestor_stat", metadata, raising=False)


class Rig:
    def __init__(self, tmp_path: Path, shared_uid: bool = False) -> None:
        table = _config_table()
        if shared_uid:
            table["agents"]["claude@dev"]["uid"] = PI_UID
        self.config = parse(table)
        self.clock = Clock()
        self.journal = LocalJournal(
            tmp_path / "state" / "journal.sqlite3",
            box="dev",
            principals={agent.principal: agent.uid for agent in self.config.agents.values()},
            clock=self.clock,
        )
        self.grants = Grants(self.clock)
        self.runs = Runs()
        self.controller = Controller(
            self.config, self.journal, self.grants, self.runs, self.clock, lambda: self.clock.monotonic
        )

    def ask(self, op: Operation, run_id: str, uid: int = PI_UID, fence: str = FENCE) -> dict[str, object]:
        request = Request(v=1, request_id=str(uuid4()), op=op, run_id=run_id, fence=fence)
        return self.controller.handle(request, uid).as_json()


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_start_launches_once_inside_the_grant_and_retries_do_not_relaunch(rig: Rig) -> None:
    run_id = str(uuid4())

    first = rig.ask("start", run_id)
    again = rig.ask("start", run_id)

    assert first["status"] == "running", first
    assert again["status"] == "running", again
    assert [spec.unit for spec in rig.runs.launched] == [unit_name(run_id)], rig.runs.launched
    spec = rig.runs.launched[0]
    assert spec.agent.principal == "pi@dev" and spec.agent.uid == PI_UID
    assert spec.runtime_max_seconds == pytest.approx(900.0 - 5), spec


def test_the_grant_round_trip_is_charged_against_the_run(rig: Rig) -> None:
    rig.grants.rtt = 30.0

    rig.ask("start", str(uuid4()))

    assert rig.runs.launched[0].runtime_max_seconds == pytest.approx(900.0 - 30.0 - 5), rig.runs.launched


def test_the_utc_window_end_caps_the_run(rig: Rig) -> None:
    rig.grants.grant = ServerGrant("pi@dev", "dev", "admitted", 900.0, 1_000.0 + 120.0)

    rig.ask("start", str(uuid4()))

    assert rig.runs.launched[0].runtime_max_seconds == pytest.approx(120.0 - 5), rig.runs.launched


@pytest.mark.parametrize(
    ("grant", "uid", "reason"),
    [
        pytest.param(
            ServerGrant("pi@dev", "vault", "admitted", 900, 9e9), PI_UID, "wrong_box", id="wrong-box"
        ),
        pytest.param(
            ServerGrant("claude@dev", "dev", "admitted", 900, 9e9),
            PI_UID,
            "wrong_principal",
            id="other-agents-run",
        ),
        pytest.param(
            ServerGrant("pi@dev", "dev", "stopping", 900, 9e9), PI_UID, "not_admissible", id="stopping"
        ),
        pytest.param(
            ServerGrant("pi@dev", "dev", "admitted", 10, 9e9), PI_UID, "insufficient_time", id="too-short"
        ),
        pytest.param(
            ServerGrant("pi@dev", "dev", "admitted", 900, 9e9),
            STRANGER_UID,
            "peer_not_configured",
            id="stranger",
        ),
    ],
)
def test_inadmissible_starts_launch_nothing(rig: Rig, grant: ServerGrant, uid: int, reason: str) -> None:
    rig.grants.grant = grant

    answer = rig.ask("start", str(uuid4()), uid=uid)

    assert answer["status"] == "refused" and answer["reason"] == reason, answer
    assert rig.runs.launched == [] and rig.journal.active() == (), "an inadmissible start left state behind"


def test_an_unreachable_board_launches_nothing(rig: Rig) -> None:
    rig.grants.failure = OSError("board down")

    answer = rig.ask("start", str(uuid4()))

    assert answer == {**answer, "status": "refused", "reason": "grant_unavailable"}, answer
    assert rig.runs.launched == []


def test_a_failed_launch_is_quarantined_and_keeps_the_box(rig: Rig) -> None:
    rig.runs.launch_failure = OSError("systemd-run timed out")
    first = rig.ask("start", str(uuid4()))
    rig.runs.launch_failure = None

    second = rig.ask("start", str(uuid4()))

    assert first["status"] == "quarantined" and first["reason"] == "launch_uncertain", first
    assert second == {**second, "status": "refused", "reason": "capacity"}, second


def test_a_second_run_cannot_start_while_one_is_active(rig: Rig) -> None:
    rig.ask("start", str(uuid4()))
    rig.grants.grant = ServerGrant("claude@dev", "dev", "admitted", 900.0, 9e9)

    answer = rig.ask("start", str(uuid4()), uid=CLAUDE_UID)

    assert answer["status"] == "refused" and answer["reason"] == "capacity", answer
    assert len(rig.runs.launched) == 1


def test_stop_kills_the_derived_unit_and_reports_proof(rig: Rig) -> None:
    run_id = str(uuid4())
    rig.ask("start", run_id)

    answer = rig.ask("stop", run_id)

    assert rig.runs.stopped == [unit_name(run_id)], rig.runs.stopped
    assert answer["status"] == "stopped", answer


def test_stop_without_proof_stays_stopping(rig: Rig) -> None:
    run_id = str(uuid4())
    rig.ask("start", run_id)
    rig.runs.observation = Observation(unit_active=False, cgroup_populated=None)

    answer = rig.ask("stop", run_id)

    assert answer["status"] == "stopping", answer


def test_another_agent_or_a_wrong_fence_cannot_stop_or_read_a_run(rig: Rig) -> None:
    run_id = str(uuid4())
    rig.ask("start", run_id)

    by_other = rig.ask("stop", run_id, uid=CLAUDE_UID)
    with_bad_fence = rig.ask("stop", run_id, fence="g" * 43)
    peek = rig.ask("status", run_id, uid=CLAUDE_UID)

    assert {by_other["reason"], with_bad_fence["reason"], peek["reason"]} == {"unknown_run"}
    assert rig.runs.stopped == [], rig.runs.stopped


def test_status_reports_the_journal_state(rig: Rig) -> None:
    run_id = str(uuid4())
    rig.ask("start", run_id)

    assert rig.ask("status", run_id)["status"] == "running"


class Echo:
    def __init__(self) -> None:
        self.seen: list[tuple[Request, int]] = []

    def handle(self, request: Request, peer_uid: int) -> Any:
        self.seen.append((request, peer_uid))
        return _Answer(request)


class _Answer:
    def __init__(self, request: Request) -> None:
        self.request = request

    def as_json(self) -> dict[str, object]:
        return {
            "v": 1,
            "request_id": self.request.request_id,
            "run_id": self.request.run_id,
            "status": "running",
        }


def _exchange(frame: bytes, handler: Echo) -> dict[str, object]:
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with ours, theirs:
        theirs.sendall(frame)
        theirs.shutdown(socket.SHUT_WR)
        server.answer_one(ours, handler)
        ours.close()
        reply: dict[str, object] = json.loads(theirs.recv(65536))
        return reply


def test_the_server_passes_the_kernel_peer_uid_not_a_claimed_one() -> None:
    handler = Echo()
    request = Request(v=1, request_id=str(uuid4()), op="status", run_id=str(uuid4()), fence=FENCE)

    reply = _exchange(request.encode(), handler)

    assert reply["status"] == "running", reply
    assert handler.seen[0][1] == os.getuid(), handler.seen


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b'{"v":1}\n', id="missing-fields"),
        pytest.param(b"x" * 9000 + b"\n", id="oversized"),
        pytest.param(b'{"v":1,"request_id":"a","op":"start","run_id":"b","fence":"c"}', id="no-terminator"),
    ],
)
def test_the_server_refuses_malformed_frames_without_calling_the_controller(frame: bytes) -> None:
    handler = Echo()

    reply = _exchange(frame, handler)

    assert reply["status"] == "refused", reply
    assert handler.seen == [], "a malformed frame reached the controller"


def test_only_a_single_systemd_socket_is_accepted() -> None:
    with pytest.raises(server.SocketError):
        server.inherited_listener({"LISTEN_PID": "1", "LISTEN_FDS": "1"})
    with pytest.raises(server.SocketError):
        server.inherited_listener({"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "2"})


def test_on_a_shared_account_the_grant_names_the_principal_and_either_kind_can_stop_it(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, shared_uid=True)
    rig.grants.grant = ServerGrant("claude@dev", "dev", "admitted", 900.0, 9e9)
    run_id = str(uuid4())

    started = rig.ask("start", run_id, uid=PI_UID)
    stopped = rig.ask("stop", run_id, uid=PI_UID)

    assert started["status"] == "running", started
    assert rig.runs.launched[0].agent.principal == "claude@dev", rig.runs.launched
    assert stopped["status"] == "stopped", stopped


def test_the_grant_round_trip_is_measured_on_a_clock_that_counts_suspend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    clocks: list[int] = []

    def read_clock(clock: int) -> float:
        clocks.append(clock)
        return 0.0

    monkeypatch.setattr(time, "clock_gettime", read_clock)
    default_clock = Controller(rig.config, rig.journal, rig.grants, rig.runs, rig.clock)

    default_clock.handle(
        Request(v=1, request_id=str(uuid4()), op="start", run_id=str(uuid4()), fence=FENCE), PI_UID
    )

    assert clocks and set(clocks) == {time.CLOCK_BOOTTIME}, clocks
