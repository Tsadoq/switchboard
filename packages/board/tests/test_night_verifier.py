import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from agent_board.night.board import SignedBoard
from agent_board.night_authority import AuthorityHTTP, NightAuthorityClient
from agent_board.night_inspection import unit_cgroup
from board.app import create_app
from board.night import NightAdmission, Run, window_on
from board.settings import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class Case:
    client: TestClient
    app: FastAPI
    service: Any
    agent: Any
    path: Path
    now: float
    journal: str

    def send(self, action: str, target: str, payload: dict[str, Any], actor: Any = None) -> Any:
        raw, signature = (actor or self.service).envelope(action, target, payload)
        return self.client.post(
            "/api/night/verifier",
            json={"envelope": base64.urlsafe_b64encode(raw).decode(), "signature": signature},
        )

    def run(self, principal: str = "pi@dev") -> Run:
        assert self.client.portal is not None
        admission = NightAdmission(self.app.state.db)
        self.client.portal.call(admission.set_enabled, True)
        self.client.portal.call(admission.enroll, principal, True)
        return self.client.portal.call(admission.admit, principal, self.now)

    def binding(self, run: Run, counter: int = 0) -> dict[str, Any]:
        return {
            "fence": run.fence,
            "journal_id": self.journal,
            "counter": counter,
            "receipt_id": str(uuid4()),
            "boot_id": str(uuid4()),
            "invocation_id": "a" * 32,
            "cgroup": unit_cgroup(f"switchboard-night-{run.id}.service"),
        }


@pytest.fixture
def case(settings: Settings, signer: Any, tmp_path: Path) -> Iterator[Case]:
    service, agent = signer("verifier@dev"), signer("pi@dev")
    blob = service.allowed_line.split()[-1]
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(blob)).digest()
    ).decode().rstrip("=")
    path = tmp_path / "verifiers.json"
    path.write_text(json.dumps({service.principal: {"box": "dev", "fingerprint": fingerprint}}))
    app = create_app(replace(settings, night_verifiers_path=path))
    app.state.registry.add_line(service.allowed_line)
    app.state.registry.add_line(agent.allowed_line)
    now = window_on(date(2026, 11, 11)).start + 60
    app.state.night_verifier._clock = lambda: now
    with TestClient(app) as client:
        case = Case(client, app, service, agent, settings.db_path, now, str(uuid4()))
        response = case.send(
            "night.bootstrap", "self", {"journal_id": case.journal, "receipt_id": str(uuid4())}
        )
        assert response.status_code == 200, response.text
        yield case


@dataclass(frozen=True)
class ClientConfig:
    principal: str = "verifier@dev"
    box: str = "dev"
    namespace: str = "switchboard"
    board_url: str = "https://board.example.com"
    ca_path: Path | None = None
    signer_key: Path = Path("/etc/switchboard-night/verifier_ed25519")


def test_real_signed_client_reaches_role_scoped_server_and_settles(case: Case) -> None:
    client = NightAuthorityClient(
        ClientConfig(), http=cast(AuthorityHTTP, case.client), signer=case.service.sign
    )
    board = SignedBoard("dev", client)
    assert board.state().journal_id == case.journal
    assert board.bootstrap({"journal_id": case.journal, "receipt_id": str(uuid4())}).counter == 0
    run = case.run()
    assert board.fetch_grant(run.id, run.fence).principal == "pi@dev"
    binding = case.binding(run)
    assert board.binding(run.id, binding).counter == 1
    evidence = binding | {
        "counter": 1,
        "receipt_id": str(uuid4()),
        "outcome": "empty",
        "runtime_seconds": None,
        "reason": "completed",
    }
    assert board.evidence(run.id, evidence).counter == 2
    state = board.state()
    assert state.counter == 2 and state.active_run_ids == frozenset()


def test_bootstrap_retry_returns_fresh_state_not_a_stale_counter(case: Case) -> None:
    payload = {"journal_id": case.journal, "receipt_id": str(uuid4())}
    assert case.send("night.bootstrap", "self", payload).json()["counter"] == 0
    run = case.run()
    assert case.send("night.binding", run.id, case.binding(run)).json()["counter"] == 1
    assert case.send("night.bootstrap", "self", payload).json()["counter"] == 1


def test_grant_uses_authoritative_principal_window_budget_and_enablement(case: Case) -> None:
    run = case.run()
    response = case.send("night.grant", run.id, {"fence": run.fence})
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "principal": "pi@dev",
        "box": "dev",
        "state": "admitted",
        "remaining_seconds": 1200,
        "window_end_utc": window_on(date(2026, 11, 11)).end,
    }
    assert case.client.portal is not None
    case.client.portal.call(NightAdmission(case.app.state.db).set_enabled, False)
    assert case.send("night.grant", run.id, {"fence": run.fence}).json()["state"] == "disabled"


def test_agent_signature_and_foreign_box_cannot_obtain_grants(case: Case) -> None:
    run = case.run()
    rejected = case.send("night.grant", run.id, {"fence": run.fence}, case.agent)
    assert rejected.status_code == 401 and rejected.json()["reason"] == "forbidden_role"
    foreign = case.run("pi@vault")
    assert case.send("night.grant", foreign.id, {"fence": foreign.fence}).status_code == 409
    assert case.send("night.grant", run.id, {"fence": "b" * 43}).status_code == 409


def test_binding_and_evidence_are_fenced_atomic_and_idempotent(case: Case) -> None:
    run = case.run()
    body = case.binding(run)
    first = case.send("night.binding", run.id, body)
    assert first.status_code == 200 and first.json()["counter"] == 1
    assert case.send("night.binding", run.id, body).json() == first.json()
    different = body | {"receipt_id": str(uuid4()), "counter": 1, "invocation_id": "b" * 32}
    assert case.send("night.binding", run.id, different).status_code == 409
    evidence = body | {
        "receipt_id": str(uuid4()),
        "counter": 1,
        "outcome": "empty",
        "runtime_seconds": None,
        "reason": "completed",
    }
    result = case.send("night.evidence", run.id, evidence)
    assert (
        result.status_code == 200 and result.json()["state"] == "finished" and result.json()["counter"] == 2
    )
    assert case.send("night.evidence", run.id, evidence).json() == result.json()
    with sqlite3.connect(case.path) as conn:
        assert conn.execute(
            "SELECT spent, reserved FROM night_budget WHERE principal = 'pi@dev'"
        ).fetchone() == (1200, 0)
        assert conn.execute("SELECT counter FROM night_verifier_state WHERE box = 'dev'").fetchone()[0] == 2


def test_no_prior_binding_no_release_and_no_counter_mutation(case: Case) -> None:
    run = case.run()
    evidence = case.binding(run) | {"outcome": "empty", "runtime_seconds": 0, "reason": "completed"}
    assert case.send("night.evidence", run.id, evidence).status_code == 409
    with sqlite3.connect(case.path) as conn:
        assert (
            conn.execute("SELECT state FROM night_runs WHERE id = ?", (run.id,)).fetchone()[0] == "admitted"
        )
        assert conn.execute("SELECT counter FROM night_verifier_state WHERE box = 'dev'").fetchone()[0] == 0


def test_remote_journal_identity_cannot_be_reset_and_bad_counters_cannot_start(case: Case) -> None:
    run = case.run()
    assert (
        case.send(
            "night.bootstrap", "self", {"journal_id": str(uuid4()), "receipt_id": str(uuid4())}
        ).status_code
        == 409
    )
    assert case.send("night.binding", run.id, case.binding(run, 1)).status_code == 409
    state = case.send("night.state", "self", {}).json()
    assert state["journal_id"] == case.journal and state["counter"] == 0
    assert state["active"][0]["id"] == run.id


def test_overrun_evidence_cannot_be_erased_by_smaller_retry(case: Case) -> None:
    run = case.run()
    body = case.binding(run)
    assert case.send("night.binding", run.id, body).status_code == 200
    evidence = body | {
        "receipt_id": str(uuid4()),
        "counter": 1,
        "outcome": "empty",
        "runtime_seconds": 1201,
        "reason": "deadline",
    }
    assert case.send("night.evidence", run.id, evidence).json()["state"] == "quarantined"
    smaller = evidence | {"receipt_id": str(uuid4()), "counter": 2, "runtime_seconds": 1}
    assert case.send("night.evidence", run.id, smaller).json()["state"] == "quarantined"
    with sqlite3.connect(case.path) as conn:
        assert conn.execute(
            "SELECT elapsed, charged, state FROM night_runs WHERE id = ?", (run.id,)
        ).fetchone() == (1201, 1200, "quarantined")


@pytest.mark.parametrize(
    "change",
    [
        {"invocation_id": "0" * 32},
        {"cgroup": "/user.slice/escaped"},
        {"counter": True},
        {"command": "ignored"},
    ],
)
def test_bad_binding_never_starts_or_advances_counter(case: Case, change: dict[str, Any]) -> None:
    run = case.run()
    assert case.send("night.binding", run.id, case.binding(run) | change).status_code == 409
    assert case.send("night.state", "self", {}).json()["counter"] == 0


def _capture_setup(case: Case, count: int = 3) -> tuple[Run, dict[str, Any]]:
    run = case.run()
    binding = case.binding(run)
    assert case.send("night.binding", run.id, binding).status_code == 200
    with sqlite3.connect(case.path) as conn:
        conn.executemany(
            "INSERT INTO wakes (principal, reason, source_ref, summary, created_at) "
            "VALUES ('pi@dev', 'timer', 'profile:pi@dev', ?, ?)",
            [(f"task {index}", case.now - 10) for index in range(count)],
        )
    return run, binding | {"counter": 1, "receipt_id": str(uuid4())}


def test_capture_is_durable_idempotent_and_never_an_ack_or_runtime_extension(case: Case) -> None:
    run, payload = _capture_setup(case)
    first = case.send("night.capture", run.id, payload)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["counter"] == 2 and data["deadline"] == run.deadline
    assert [wake["summary"] for wake in data["wakes"]] == ["task 0", "task 1", "task 2"]
    with sqlite3.connect(case.path) as conn:
        conn.execute("UPDATE wakes SET summary = 'changed after snapshot'")
    assert case.send("night.capture", run.id, payload).json() == data
    assert case.send("night.state", "self", {}).json()["counter"] == 2
    with sqlite3.connect(case.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM night_captured_wakes").fetchone()[0] == 3
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM wakes WHERE acked_at IS NOT NULL OR leased_until IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT deadline FROM night_runs WHERE id = ?", (run.id,)).fetchone()[0]
            == run.deadline
        )
    assert case.client.portal is not None
    assert case.client.portal.call(case.app.state.wakes.ack, "pi@dev", [w["id"] for w in data["wakes"]]) == 0


def test_capture_batches_do_not_redeliver_current_run_wakes(case: Case) -> None:
    run, payload = _capture_setup(case, 30)
    first = case.send("night.capture", run.id, payload).json()
    second = case.send("night.capture", run.id, payload | {"receipt_id": str(uuid4()), "counter": 2}).json()
    assert len(first["wakes"]) == 20 and len(second["wakes"]) == 10
    assert not {w["id"] for w in first["wakes"]} & {w["id"] for w in second["wakes"]}


@pytest.mark.parametrize(
    "change",
    [
        {"counter": True},
        {"counter": 0},
        {"journal_id": str(uuid4())},
        {"boot_id": str(uuid4())},
        {"invocation_id": "b" * 32},
        {"cgroup": "/user.slice/outside"},
        {"fence": "b" * 43},
        {"principal": "pi@vault"},
        {"command": "ignored"},
    ],
)
def test_capture_refuses_bad_authority_without_consuming_or_advancing(
    case: Case, change: dict[str, Any]
) -> None:
    run, payload = _capture_setup(case)
    assert case.send("night.capture", run.id, payload | change).status_code == 409
    assert case.send("night.state", "self", {}).json()["counter"] == 1
    with sqlite3.connect(case.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM night_wake_captures").fetchone()[0] == 0


def test_agent_signature_cannot_capture(case: Case) -> None:
    run, payload = _capture_setup(case)
    response = case.send("night.capture", run.id, payload, case.agent)
    assert response.status_code == 401 and response.json()["reason"] == "forbidden_role"


def test_capture_changed_receipt_is_rejected(case: Case) -> None:
    run, payload = _capture_setup(case)
    assert case.send("night.capture", run.id, payload).status_code == 200
    response = case.send("night.capture", run.id, payload | {"counter": 2})
    assert response.status_code == 409 and response.json()["reason"] == "receipt_conflict"


@pytest.mark.parametrize("state", ["stopping", "quarantined", "finished"])
def test_capture_refuses_nonrunning_run(case: Case, state: str) -> None:
    run, payload = _capture_setup(case)
    with sqlite3.connect(case.path) as conn:
        conn.execute("UPDATE night_runs SET state = ? WHERE id = ?", (state, run.id))
    assert case.send("night.capture", run.id, payload).status_code == 409
    assert case.send("night.state", "self", {}).json()["counter"] == 1


def test_capture_cannot_take_human_work_or_bypass_pending_human_priority(case: Case) -> None:
    run, payload = _capture_setup(case)
    with sqlite3.connect(case.path) as conn:
        conn.execute("UPDATE wakes SET origin_kind = 'human' WHERE id = (SELECT MIN(id) FROM wakes)")
    response = case.send("night.capture", run.id, payload)
    assert response.status_code == 409 and response.json()["reason"] == "human_priority"
    assert case.send("night.state", "self", {}).json()["counter"] == 1


def test_capture_refuses_expired_run_and_preserves_counter(case: Case) -> None:
    run, payload = _capture_setup(case)
    case.app.state.night_verifier._clock = lambda: run.deadline
    response = case.send("night.capture", run.id, payload)
    assert response.status_code == 409 and response.json()["reason"] == "expired"
    assert case.send("night.state", "self", {}).json()["counter"] == 1


def test_capture_retry_after_off_is_only_a_snapshot_not_fresh_authority(case: Case) -> None:
    run, payload = _capture_setup(case)
    first = case.send("night.capture", run.id, payload).json()
    assert case.client.portal is not None
    case.client.portal.call(NightAdmission(case.app.state.db).set_enabled, False)
    assert case.send("night.capture", run.id, payload).json() == first
    assert case.send("night.grant", run.id, {"fence": run.fence}).json()["state"] == "disabled"
    assert (
        case.send("night.capture", run.id, payload | {"receipt_id": str(uuid4()), "counter": 2}).status_code
        == 409
    )


def test_unacknowledged_capture_can_only_be_recaptured_after_finished_run(case: Case) -> None:
    run, payload = _capture_setup(case, 1)
    first = case.send("night.capture", run.id, payload).json()
    empty = case.send("night.capture", run.id, payload | {"receipt_id": str(uuid4()), "counter": 2}).json()
    assert empty["wakes"] == []
    evidence = payload | {
        "receipt_id": str(uuid4()),
        "counter": 3,
        "outcome": "empty",
        "runtime_seconds": 0,
        "reason": "completed",
    }
    assert case.send("night.evidence", run.id, evidence).json()["state"] == "finished"
    replacement = case.run()
    binding = case.binding(replacement, counter=4)
    assert case.send("night.binding", replacement.id, binding).status_code == 200
    next_payload = binding | {"receipt_id": str(uuid4()), "counter": 5}
    recaptured = case.send("night.capture", replacement.id, next_payload).json()
    assert recaptured["wakes"] == first["wakes"]
    with sqlite3.connect(case.path) as conn:
        assert conn.execute("SELECT acked_at FROM wakes").fetchone()[0] is None


def test_capture_does_not_take_outstanding_ordinary_lease(case: Case) -> None:
    run, payload = _capture_setup(case)
    with sqlite3.connect(case.path) as conn:
        conn.execute("UPDATE wakes SET leased_until = ?", (case.now + 300,))
    assert case.send("night.capture", run.id, payload).json()["wakes"] == []


def test_capture_truncation_is_explicit_and_oversized_metadata_rolls_back(case: Case) -> None:
    run, payload = _capture_setup(case, 1)
    with sqlite3.connect(case.path) as conn:
        conn.execute("UPDATE wakes SET summary = ?", ("x" * 4000,))
    data = case.send("night.capture", run.id, payload).json()
    assert len(data["wakes"][0]["summary"]) == 1024
    assert data["wakes"][0]["summary_truncated"] is True
    with sqlite3.connect(case.path) as conn:
        conn.execute(
            "INSERT INTO wakes (principal, reason, source_ref, summary, created_at) "
            "VALUES ('pi@dev', 'timer', ?, 'oversized', ?)",
            ("x" * 40000, case.now),
        )
    response = case.send("night.capture", run.id, payload | {"receipt_id": str(uuid4()), "counter": 2})
    assert response.status_code == 409 and response.json()["reason"] == "wake_metadata_too_large"
    assert case.send("night.state", "self", {}).json()["counter"] == 2
