import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_board.night import effective_queue as queue
from agent_board.night.ledger import Ledger
from agent_board.night.ledger_binding import BindingClaim
from agent_board.night_inspection import unit_cgroup

if TYPE_CHECKING:
    from packages.board.tests.test_night_verifier import Case, case
else:
    from test_night_verifier import Case, case

__all__ = ["case"]


def fixture(path: Path, journal: str) -> queue.ExpectedControl:
    generation = str(uuid4())
    Ledger(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE identity SET journal_id=?", (journal,))
        conn.execute(queue.EFFECTIVE_QUEUE_DDL)
        conn.execute(
            "INSERT INTO effective_queue(singleton,generation,revision,active_seq) VALUES(1,?,0,NULL)",
            (generation,),
        )
        conn.execute(f"PRAGMA user_version={queue.QUEUE_VERSION}")
        conn.execute("COMMIT")
    return queue.ExpectedControl(journal, generation, 0, None, 0)


def usable(result: queue.QueueResult) -> queue.QueueResult:
    assert result.refusal is None and result.fault is None and result.control is not None, result
    return result


def answer(case: Case, run_id: str, published: queue.QueueResult) -> dict[str, object]:
    assert published.selected is not None
    action = f"night.{published.selected.action}"
    response = case.send(action, run_id, json.loads(published.selected.payload))
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def claim_from(receipt_id: str, body: dict[str, object]) -> queue.OrdinaryAckClaim:
    journal, counter, state = body["journal_id"], body["counter"], body["state"]
    assert isinstance(journal, str) and isinstance(counter, int) and isinstance(state, str)
    return queue.OrdinaryAckClaim(receipt_id, journal, counter, state)


def test_cached_historical_server_answers_are_not_current_grants(case: Case) -> None:
    run = case.run()
    with TemporaryDirectory(prefix="eqr.", dir=Path.home()) as directory:
        path = Path(directory) / "ledger.sqlite"
        empty = fixture(path, case.journal)
        claim = BindingClaim(
            run.id, 1000, str(uuid4()), "a" * 32, unit_cgroup(f"switchboard-night-{run.id}.service")
        )
        binding = queue.BindingPublication(str(uuid4()), run.id, run.fence, claim)
        published = usable(queue.publish_ordinary(path, expected=empty, publication=binding))
        assert published.control is not None
        active = published.control

        first = answer(case, run.id, published)
        assert first == {"journal_id": case.journal, "counter": 1, "state": "running"}
        replayed = answer(case, run.id, published)
        assert replayed == first, "the server answers a repeated request from its cache"
        assert case.send("night.state", "self", {}).json()["counter"] == 1

        binding_ack = claim_from(binding.receipt_id, first)
        acked = usable(queue.acknowledge_ordinary(path, expected=active, ack=binding_ack))
        assert acked.control is not None
        bound = acked.control

        again = queue.acknowledge_ordinary(
            path, expected=active, ack=claim_from(binding.receipt_id, replayed)
        )
        assert again.refusal == queue.Refusal.CONTROL_MISMATCH and again.control is None
        on_empty = queue.acknowledge_ordinary(
            path, expected=bound, ack=claim_from(binding.receipt_id, replayed)
        )
        assert on_empty.refusal == queue.Refusal.NOTHING_SELECTED and on_empty.control is None

        evidence = queue.EvidencePublication(
            str(uuid4()), run.id, run.fence, claim, "empty", "completed", None
        )
        pending = usable(queue.publish_ordinary(path, expected=bound, publication=evidence))
        assert pending.control is not None
        for stale in (
            claim_from(binding.receipt_id, replayed),
            claim_from(evidence.receipt_id, replayed),
        ):
            result = queue.acknowledge_ordinary(path, expected=pending.control, ack=stale)
            assert result.refusal == queue.Refusal.ACK_REFUSED and result.control is None

        current = answer(case, run.id, pending)
        assert current == {"journal_id": case.journal, "counter": 2, "state": "finished"}
        done = usable(
            queue.acknowledge_ordinary(
                path, expected=pending.control, ack=claim_from(evidence.receipt_id, current)
            )
        )
        assert done.control == queue.ExpectedControl(case.journal, empty.generation, 4, None, 2)
