import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from agent_board.night import capture_queue as cq
from agent_board.night import effective_queue as eq
from agent_board.night.assignment import NightPolicy, build_assignment, encode, parse_wakes
from agent_board.night.journal_latch import MutationOutcome
from agent_board.night.ledger_binding import BindingClaim
from agent_board.night_inspection import unit_cgroup

if TYPE_CHECKING:
    from packages.board.tests.test_night_effective_queue_replay import fixture
    from packages.board.tests.test_night_verifier import Case, case
else:
    from test_night_effective_queue_replay import fixture
    from test_night_verifier import Case, case

__all__ = ["case"]
POLICY = NightPolicy("sb-runner", "a" * 40, "claude", None, "default", 600)


class Local:
    def __init__(self, case: Case, path: Path) -> None:
        self.case, self.path = case, path
        self.run = case.run()
        with sqlite3.connect(case.path) as conn:
            conn.executemany(
                "INSERT INTO wakes (principal, reason, source_ref, summary, created_at) "
                "VALUES ('pi@dev', 'timer', 'profile:pi@dev', ?, ?)",
                [(f"task {index}", case.now - 10) for index in range(2)],
            )
        empty = fixture(path, case.journal)
        self.claim = BindingClaim(
            self.run.id, 1000, str(uuid4()), "a" * 32, unit_cgroup(f"switchboard-night-{self.run.id}.service")
        )
        binding = eq.BindingPublication(str(uuid4()), self.run.id, self.run.fence, self.claim)
        published = eq.publish_ordinary(path, expected=empty, publication=binding)
        assert published.control is not None and published.selected is not None
        answer = self.send("night.binding", json.loads(published.selected.payload))
        ack = eq.OrdinaryAckClaim(
            binding.receipt_id, answer["journal_id"], answer["counter"], answer["state"]
        )
        bound = eq.acknowledge_ordinary(path, expected=published.control, ack=ack)
        assert bound.control is not None
        self.choices = cq.CaptureChoices("pi@dev", float(self.run.deadline + 100), POLICY)
        self.capture = cq.CapturePublication(
            str(uuid4()), self.run.id, self.run.fence, self.claim, self.choices
        )
        captured = cq.publish_capture(path, expected=bound.control, publication=self.capture)
        assert captured.control is not None and captured.episode is not None
        self.parent_token, self.parent = captured.control, captured.episode

    def send(self, action: str, payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
        response = self.case.send(action, self.run.id, payload)
        assert response.status_code == status, response.text
        body: dict[str, Any] = response.json()
        return body

    def abort(self) -> tuple[eq.ExpectedControl, cq.CaptureEpisode]:
        publication = cq.CaptureAbortPublication(str(uuid4()), self.run.id, self.capture.receipt_id)
        result = cq.publish_capture_abort(self.path, expected=self.parent_token, publication=publication)
        assert result.control is not None and result.episode is not None and result.episode.abort_payload
        return result.control, result.episode


def packet(body: object) -> bytes:
    return cq._canonical(body).encode()


@pytest.fixture
def local(case: Case) -> Any:
    with TemporaryDirectory(prefix="cqs.", dir=Path.home()) as directory:
        yield Local(case, Path(directory) / "ledger.sqlite")


def test_aborted_branch_tombstones_a_delayed_parent_and_settles_nothing(local: Local) -> None:
    token, episode = local.abort()
    proof = local.send("night.capture_abort", json.loads(episode.abort_payload or ""))
    assert (proof["outcome"], proof["counter"], proof["capture"]) == ("aborted", 2, None)

    resolved = cq.resolve_capture_abort(
        local.path,
        expected=token,
        claim=cq.AbortResolutionClaim(episode.abort_receipt_id or "", packet(proof)),
    )
    assert resolved.completion == cq.AbortedCompletion(packet(proof).decode())
    assert resolved.control is not None and resolved.control.acked_counter == 2

    late = local.send("night.capture", json.loads(local.parent.capture_payload), status=409)
    assert late["reason"] == "capture_aborted"
    forged = {
        "journal_id": local.case.journal,
        "counter": 2,
        "capture_id": local.capture.receipt_id,
        "run_id": local.run.id,
        "principal": "pi@dev",
        "deadline": local.run.deadline,
        "wakes": [],
    }
    stale = cq.acknowledge_capture(
        local.path,
        expected=local.parent_token,
        claim=cq.CaptureAckClaim(local.capture.receipt_id, packet(forged)),
    )
    assert stale.refusal == cq.Refusal.CONTROL_MISMATCH and stale.completion is None
    assert local.case.send("night.state", "self", {}).json()["active"][0]["state"] == "running"


def test_applied_branch_with_a_lost_reply_returns_history_only_and_the_cached_snapshot_grants_nothing(
    local: Local,
) -> None:
    snapshot = local.send("night.capture", json.loads(local.parent.capture_payload))
    token, episode = local.abort()

    late_parent = cq.CaptureAckClaim(local.capture.receipt_id, packet(snapshot))
    old = cq.acknowledge_capture(local.path, expected=local.parent_token, claim=late_parent)
    assert old.refusal == cq.Refusal.CONTROL_MISMATCH and old.completion is None
    wrong = cq.acknowledge_capture(local.path, expected=token, claim=late_parent)
    assert wrong.refusal == cq.Refusal.WRONG_ACTION and wrong.completion is None

    proof = local.send("night.capture_abort", json.loads(episode.abort_payload or ""))
    assert proof["outcome"] == "applied" and proof["capture"] == snapshot and proof["counter"] == 2
    resolved = cq.resolve_capture_abort(
        local.path,
        expected=token,
        claim=cq.AbortResolutionClaim(episode.abort_receipt_id or "", packet(proof)),
    )
    assert type(resolved.completion) is cq.AppliedAfterAbortCompletion
    assert not hasattr(resolved.completion, "assignment")
    assignment = build_assignment(
        run_id=local.run.id,
        principal="pi@dev",
        wakes=parse_wakes(snapshot["wakes"]),
        policy=POLICY,
        deadline_utc=min(float(snapshot["deadline"]), local.choices.cutoff_utc),
    )
    with closing(sqlite3.connect(local.path)) as conn:
        assert conn.execute("SELECT assignment FROM captures").fetchone()[0] == (
            None if assignment is None else encode(assignment)
        )

    cached = local.send("night.capture", json.loads(local.parent.capture_payload))
    assert cached == snapshot
    assert resolved.control is not None
    for expected, reason in (
        (local.parent_token, cq.Refusal.CONTROL_MISMATCH),
        (resolved.control, cq.Refusal.NOTHING_SELECTED),
    ):
        replay = cq.acknowledge_capture(local.path, expected=expected, claim=late_parent)
        assert replay.refusal == reason and replay.completion is None


def test_a_server_applied_capture_with_a_failed_local_commit_is_unknown_with_no_completion(
    local: Local, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = local.send("night.capture", json.loads(local.parent.capture_payload))
    token, episode = local.abort()
    proof = local.send("night.capture_abort", json.loads(episode.abort_payload or ""))
    assert proof["outcome"] == "applied" and proof["capture"] == snapshot
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "COMMIT":
                raise sqlite3.OperationalError("injected commit failure")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    result = cq.resolve_capture_abort(
        local.path,
        expected=token,
        claim=cq.AbortResolutionClaim(episode.abort_receipt_id or "", packet(proof)),
    )
    monkeypatch.undo()

    assert result.mutation == MutationOutcome.UNKNOWN and result.fault == cq.Fault.SQLITE_FAILURE
    assert result.completion is None and result.control is None and result.episode is None
