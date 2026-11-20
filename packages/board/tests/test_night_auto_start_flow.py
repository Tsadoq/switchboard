import base64
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest
from agent_board.night.assignment import decode
from agent_board.night.board import SignedBoard
from agent_board.night.cli_launch import CliSettings, cli_settings_from, launch
from agent_board.night.config import VerifierConfig, parse
from agent_board.night.controller import Controller, Observation, RunSpec
from agent_board.night.execstep import Account, exec_native
from agent_board.night.gate import GuardState, open_gate
from agent_board.night.launch import EXIT_DONE, SealedAssignments
from agent_board.night.ledger import Arm, CapturedAssignment, Ledger
from agent_board.night.sync import BoardSync
from agent_board.night_authority import AuthorityHTTP, NightAuthorityClient
from agent_board.night_inspection import Binding, EmptyObservation, Execution
from agent_board.night_state import ClockReading, LocalJournal
from board.app import create_app
from board.night import NightAdmission, window_on
from board.settings import Settings
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from packages.board.tests.test_night_verifier import ClientConfig
else:
    from test_night_verifier import ClientConfig

BOOT = "3000000a-0000-4000-8000-00000000000b"
INVOCATION = "a" * 32
GUARD = "c" * 32
UID = os.getuid()
PRINCIPAL = "pi@dev"


class Inspector:
    def capture(self, *, run_id: str, uid: int, boot_id: str, execution: Execution) -> Binding:
        return Binding(run_id, uid, boot_id, INVOCATION, execution)

    def stopped(self, binding: Binding) -> EmptyObservation:
        return EmptyObservation(binding, cgroup_present=False, unit_loaded=False)


class Runs:
    def __init__(self) -> None:
        self.launched: list[RunSpec] = []
        self.finished = False

    def launch(self, spec: RunSpec) -> None:
        self.launched.append(spec)

    def stop(self, unit: str) -> None:
        self.finished = True

    def inspect(self, unit: str) -> Observation:
        return Observation(unit_active=not self.finished, cgroup_populated=not self.finished)


class Executed(Exception):
    pass


class System:
    def __init__(self, run_id: str) -> None:
        self.cgroup = f"0::/switchboard.slice/switchboard-night.slice/switchboard-night-{run_id}.service"
        self.ids: tuple[tuple[int, int, int], tuple[int, int, int], list[int]] = ((0, 0, 0), (0, 0, 0), [0])
        self.environment: dict[str, str] = {}
        self.argv: list[str] = []

    def own_cgroup(self) -> str:
        return self.cgroup

    def boot_id(self) -> str:
        return BOOT

    def guard(self, unit: str) -> GuardState:
        return GuardState(True, GUARD)

    def boottime(self) -> float:
        return 110.0

    def utc(self) -> float:
        return 0.0

    def account(self, uid: int) -> Account:
        return Account("dev", uid, uid)

    def supplementary_groups(self, account: Account) -> list[int]:
        return []

    def drop_to(self, account: Account) -> None:
        self.ids = ((account.uid,) * 3, (account.gid,) * 3, [])

    def identity(self) -> tuple[tuple[int, int, int], tuple[int, int, int], list[int]]:
        return self.ids

    def close_inherited_descriptors(self, keep: int) -> None:
        os.set_inheritable(keep, True)

    def execve(self, path: str, argv: list[str], environment: Mapping[str, str]) -> NoReturn:
        self.argv, self.environment = argv, dict(environment)
        raise Executed


class CliHost:
    def __init__(self) -> None:
        self.ran: list[list[str]] = []
        self.cloned: list[tuple[Path, Path, str]] = []

    def uid(self) -> int:
        return UID

    def containers_trusted(self, path: Path) -> bool:
        return True

    def clone(self, source: Path, destination: Path, commit: str) -> None:
        self.cloned.append((source, destination, commit))

    def run_cli(
        self, argv: Sequence[str], cwd: Path, timeout: float, extra_env: Mapping[str, str]
    ) -> int | None:
        self.ran.append(list(argv))
        return 0

    def utc(self) -> float:
        return 0.0


class Repositories:
    def resolve(self, repository_key: str, principal: str) -> Path:
        return Path(f"/var/lib/switchboard-night-mirrors/{repository_key}.git")


@dataclass
class Flow:
    client: TestClient
    admission: NightAdmission
    db_path: Path
    now: float
    state: Path
    config: VerifierConfig
    journal: LocalJournal
    ledger: Ledger
    sync: BoardSync
    runs: Runs
    controller: Controller


def _config(state: Path) -> VerifierConfig:
    return parse(
        {
            "box": "dev",
            "principal": "verifier@dev",
            "namespace": "switchboard",
            "board_url": "https://board.example.com",
            "signer_key": "/etc/switchboard-night/verifier_ed25519",
            "state_path": str(state / "journal.sqlite3"),
            "slice": "switchboard-night.slice",
            "launcher": {"python": "/opt/switchboard-night/bin/python", "module": "agent_board.night.launch"},
            "agents": {
                PRINCIPAL: {
                    "uid": UID,
                    "login": "existing",
                    "cli": str(state / "bin" / "pi"),
                    "login_paths": [str(state / "home" / ".pi")],
                    "home": str(state / "home"),
                    "workdir_root": str(state / "work"),
                    "policy": {
                        "repository_key": "switchboard",
                        "base_commit": "a" * 40,
                        "backend": "pi",
                        "permission_mode": "auto",
                        "turn_seconds": 1200,
                    },
                }
            },
            "limits": {"memory_max": "600M", "tasks_max": 128, "cpu_quota": "100%", "stop_grace_seconds": 5},
        }
    )


@pytest.fixture
def flow(settings: Settings, signer: Any) -> Iterator[Flow]:
    service = signer("verifier@dev")
    blob = service.allowed_line.split()[-1]
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(blob)).digest()
    ).decode().rstrip("=")
    with TemporaryDirectory(prefix="flow.", dir=Path.home()) as directory:
        root = Path(directory)
        roles = root / "verifiers.json"
        roles.write_text(json.dumps({service.principal: {"box": "dev", "fingerprint": fingerprint}}))
        app = create_app(replace(settings, night_verifiers_path=roles))
        app.state.registry.add_line(service.allowed_line)
        now = window_on(date(2026, 11, 21)).start + 60
        app.state.night_verifier._clock = lambda: now
        with TestClient(app) as client:
            client_config = ClientConfig()
            authority = NightAuthorityClient(
                client_config, http=cast(AuthorityHTTP, client), signer=service.sign
            )
            board = SignedBoard("dev", authority)
            state = root / "state"
            config = _config(state)
            clock = ClockReading(BOOT, 100.0, now)
            journal = LocalJournal(
                config.state_path, box="dev", principals={PRINCIPAL: UID}, clock=lambda: clock
            )
            ledger = Ledger(state / "ledger.sqlite3")
            sync = BoardSync("dev", ledger, board, Inspector(), journal)
            sync.reconcile()
            runs = Runs()
            controller = Controller(
                config, journal, board, runs, sync, lambda: clock, lambda: 100.0, admissions=board
            )
            yield Flow(
                client,
                NightAdmission(app.state.db),
                settings.db_path,
                now,
                state,
                config,
                journal,
                ledger,
                sync,
                runs,
                controller,
            )


def test_an_enrolled_agents_held_work_runs_and_settles_without_anyone_starting_it(flow: Flow) -> None:
    assert flow.client.portal is not None
    flow.client.portal.call(flow.admission.set_enabled, True)
    flow.client.portal.call(flow.admission.enroll, PRINCIPAL, True)
    with sqlite3.connect(flow.db_path) as conn:
        conn.execute(
            "INSERT INTO wakes (principal, reason, source_ref, summary, created_at, origin_kind) "
            "VALUES (?, 'timer', 'profile:pi@dev', 'fix the flaky test', ?, 'budgeted')",
            (PRINCIPAL, flow.now - 10),
        )

    flow.controller.sweep()

    assert len(flow.runs.launched) == 1, "the admitted run was not started"
    run_id = flow.runs.launched[0].run_id
    captured = flow.ledger.captured(run_id)
    assert isinstance(captured, CapturedAssignment) and captured.assignment is not None
    assignment = decode(captured.assignment)
    assert (assignment.principal, assignment.backend, assignment.repository_key) == (
        PRINCIPAL,
        "pi",
        "switchboard",
    )
    assert "fix the flaky test" in assignment.prompt

    record = next(each for each in flow.journal.active() if each.run_id == run_id)
    flow.ledger.arm(Arm(run_id, BOOT, GUARD, record.deadline_boottime, record.deadline_utc))
    assert open_gate(
        run_id,
        ledger=flow.ledger,
        journal=flow.journal,
        environ={"INVOCATION_ID": INVOCATION},
        boot_id=lambda: BOOT,
        guard=lambda: GuardState(True, GUARD),
        boottime=lambda: 100.0,
        sleep=lambda seconds: None,
    ), "the gate stayed shut for a captured, bound and armed run"

    system = System(run_id)
    with pytest.raises(Executed):
        exec_native(
            run_id,
            config=flow.config,
            journal=flow.journal,
            ledger=flow.ledger,
            environ={"INVOCATION_ID": INVOCATION},
            system=system,
        )
    assert system.argv[-2:] == ["agent_board.night.cli_launch", run_id]

    _prepare_account(flow.state)
    settings: CliSettings = cli_settings_from(run_id, system.environment, UID)
    host = CliHost()
    assert (
        launch(
            settings,
            host=host,
            assignments=SealedAssignments(system.environment),
            repositories=Repositories(),
        )
        == EXIT_DONE
    )
    assert host.ran == [[str(flow.state / "bin" / "pi"), "-p", assignment.prompt]]
    assert host.cloned[0][1:] == (flow.state / "work" / run_id, "a" * 40)

    flow.runs.finished = True
    flow.controller.sweep()

    assert flow.journal.active() == (), "the finished run kept its slot"
    with sqlite3.connect(flow.db_path) as conn:
        state = conn.execute("SELECT state FROM night_runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert state == "finished"


def _prepare_account(state: Path) -> None:
    (state / "work").mkdir(mode=0o700)
    (state / "bin").mkdir()
    cli = state / "bin" / "pi"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o700)


def test_an_enrolled_agent_with_no_posts_still_gets_its_nightly_start(flow: Flow) -> None:
    assert flow.client.portal is not None
    flow.client.portal.call(flow.admission.set_enabled, True)
    flow.client.portal.call(flow.admission.enroll, PRINCIPAL, True)
    app: Any = flow.client.app
    slot = window_on(date(2026, 11, 21)).slot(PRINCIPAL)
    app.state.night_verifier._clock = lambda: slot + 5

    flow.controller.sweep()

    assert len(flow.runs.launched) == 1, "no run started without a pre-existing wake"
    captured = flow.ledger.captured(flow.runs.launched[0].run_id)
    assert isinstance(captured, CapturedAssignment) and captured.assignment is not None
    prompt = decode(captured.assignment).prompt
    assert '"reason": "night_initial"' in prompt
    assert "agent-board tickets list --mine" in prompt and "doing column" in prompt
