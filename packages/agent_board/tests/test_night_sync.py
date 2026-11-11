import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_board import night_state
from agent_board.night.ledger import Ledger
from agent_board.night.sync import Ack, BoardRefused, BoardState, BoardSync, BoardUnavailable, SyncError
from agent_board.night_inspection import Binding, InspectionError
from agent_board.night_state import ClockReading, Grant, LocalJournal

BOOT = str(uuid4())
UID = 2001
FENCE = "f" * 43


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


class Board:
    def __init__(self) -> None:
        self.journal_id: str | None = None
        self.counter = 0
        self.active: set[str] = set()
        self.acks: dict[str, Ack] = {}
        self.calls: list[str] = []
        self.lose_next_reply = False

    def state(self) -> BoardState:
        return BoardState("dev", self.journal_id, self.counter, frozenset(self.active))

    def bootstrap(self, payload: Mapping[str, Any]) -> Ack:
        self.calls.append("bootstrap")
        if payload["receipt_id"] in self.acks:
            return self._reply(self.acks[payload["receipt_id"]])
        if self.journal_id is not None or self.active:
            raise BoardRefused("identity_exists")
        self.journal_id = payload["journal_id"]
        return self._apply(payload["receipt_id"])

    def binding(self, run_id: str, payload: Mapping[str, Any]) -> Ack:
        self.calls.append("binding")
        if payload["receipt_id"] in self.acks:
            return self._reply(self.acks[payload["receipt_id"]])
        if payload["journal_id"] != self.journal_id or payload["counter"] != self.counter:
            raise BoardRefused("stale_counter")
        self.active.add(run_id)
        return self._apply(payload["receipt_id"])

    def evidence(self, run_id: str, payload: Mapping[str, Any]) -> Ack:
        raise AssertionError("not used here")

    def _apply(self, receipt_id: str) -> Ack:
        assert self.journal_id is not None
        self.counter += 1
        self.acks[receipt_id] = Ack(self.journal_id, self.counter)
        return self._reply(self.acks[receipt_id])

    def _reply(self, ack: Ack) -> Ack:
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise BoardUnavailable("reply lost")
        return ack


class Inspector:
    def __init__(self) -> None:
        self.failure: InspectionError | None = None

    def capture(self, *, run_id: str, uid: int, boot_id: str) -> Binding:
        if self.failure is not None:
            raise self.failure
        return Binding(run_id, uid, boot_id, "b" * 32)


class Rig:
    def __init__(self, tmp_path: Path, board: Board | None = None) -> None:
        self.state_dir = tmp_path / "state"
        self.clock = ClockReading(BOOT, 100.0, 1_000.0)
        self.journal = LocalJournal(
            self.state_dir / "journal.sqlite3",
            box="dev",
            principals={"pi@dev": UID},
            clock=lambda: self.clock,
        )
        self.board = board or Board()
        self.inspector = Inspector()
        self.ledger = Ledger(self.state_dir / "ledger.sqlite3")
        self.sync = BoardSync("dev", self.ledger, self.board, self.inspector, self.journal)

    def reserve(self) -> str:
        run_id = str(uuid4())
        self.journal.reserve(Grant(run_id, FENCE, "pi@dev", UID, BOOT, 900.0, 1_900.0))
        return run_id

    def bind(self, run_id: str) -> None:
        self.sync.bind(run_id=run_id, fence=FENCE, uid=UID, boot_id=BOOT)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_fresh_box_bootstraps_once_and_unfreezes(rig: Rig) -> None:
    assert rig.sync.reconcile() is None
    assert rig.sync.reconcile() is None

    assert rig.board.calls == ["bootstrap"], rig.board.calls
    assert rig.ledger.identity().journal_id == rig.board.journal_id is not None


def test_new_starts_are_frozen_until_reconciled(rig: Rig) -> None:
    with pytest.raises(SyncError, match="not_reconciled"):
        rig.bind(rig.reserve())


def test_a_bootstrap_whose_reply_was_lost_is_resent_identically(rig: Rig) -> None:
    rig.board.lose_next_reply = True

    first = rig.sync.reconcile()
    second = rig.sync.reconcile()

    assert first is not None and first.startswith("reconcile_failed"), first
    assert second is None, second
    assert rig.board.counter == 1, "the bootstrap was applied twice"


def test_a_wiped_ledger_does_not_pass_as_a_fresh_box(rig: Rig, tmp_path: Path) -> None:
    rig.sync.reconcile()
    (rig.state_dir / "ledger.sqlite3").unlink()

    restarted = Rig(tmp_path, rig.board)

    assert restarted.sync.reconcile() == "journal_identity_mismatch"
    assert rig.board.calls == ["bootstrap"], "a second bootstrap was attempted"


def test_a_rolled_back_ledger_is_caught_by_the_counter(rig: Rig, tmp_path: Path) -> None:
    rig.sync.reconcile()
    snapshot = tmp_path / "snapshot.sqlite3"
    shutil.copy(rig.state_dir / "ledger.sqlite3", snapshot)
    rig.bind(rig.reserve())
    shutil.copy(snapshot, rig.state_dir / "ledger.sqlite3")

    restored = BoardSync(
        "dev", Ledger(rig.state_dir / "ledger.sqlite3"), rig.board, rig.inspector, rig.journal
    )

    assert restored.reconcile() == "counter_mismatch"


def test_a_board_run_missing_from_the_local_journal_freezes(rig: Rig) -> None:
    rig.sync.reconcile()
    rig.board.active.add(str(uuid4()))

    assert rig.sync.reconcile() == "missing_local_run"


class VaultBoard(Board):
    def state(self) -> BoardState:
        return BoardState("vault", None, 0, frozenset())


def test_a_board_for_another_box_freezes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, VaultBoard())

    assert rig.sync.reconcile() == "wrong_box"


def test_binding_records_the_unit_with_the_board_and_a_lost_reply_is_replayed(rig: Rig) -> None:
    rig.sync.reconcile()
    run_id = rig.reserve()
    rig.board.lose_next_reply = True

    with pytest.raises(BoardUnavailable):
        rig.bind(run_id)
    assert rig.ledger.unacknowledged(), "the binding message was not kept for replay"
    assert rig.sync.reconcile() is None

    assert run_id in rig.board.active and rig.board.counter == 2, (rig.board.active, rig.board.counter)
    assert rig.ledger.unacknowledged() == ()


def test_a_unit_that_cannot_be_inspected_is_not_bound(rig: Rig) -> None:
    rig.sync.reconcile()
    rig.inspector.failure = InspectionError("binding_conflict")

    with pytest.raises(SyncError, match="binding_conflict"):
        rig.bind(rig.reserve())
    assert rig.board.calls == ["bootstrap"], rig.board.calls
