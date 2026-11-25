import asyncio
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from board.db import Database
from board.settings import Settings
from board.wakes.self_wakes import MAX_PENDING, SelfWakeRefused, SelfWakes, deliver
from test_self_wakes import _running_board, _thread

if TYPE_CHECKING:
    from .conftest import SignerFactory

pytestmark = pytest.mark.anyio


def _contend(monkeypatch: pytest.MonkeyPatch, participants: int) -> None:
    barrier = Barrier(participants, timeout=10)
    original = Database._run_in_transaction

    def run[T](self: Database, fn: Callable[[sqlite3.Connection], T]) -> T:
        barrier.wait()
        return original(self, fn)

    monkeypatch.setattr(Database, "_run_in_transaction", run)


@pytest.mark.parametrize("already_pending", [0, MAX_PENDING - 1])
async def test_concurrent_schedules_preserve_the_pending_cap(
    settings: Settings, signer: "SignerFactory", monkeypatch: pytest.MonkeyPatch, already_pending: int
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        thread_id = await _thread(client, pi)
    now = time.time()
    payload = {"at": datetime.fromtimestamp(now + 600, UTC).isoformat(), "note": "contention review"}
    service = SelfWakes(Database(settings.db_path), clock=lambda: now)
    for _ in range(already_pending):
        await service.schedule(pi.principal, thread_id, payload)
    contenders = MAX_PENDING + 1
    with monkeypatch.context() as patch:
        _contend(patch, contenders)
        results = await asyncio.gather(
            *(
                SelfWakes(Database(settings.db_path), clock=lambda: now).schedule(
                    pi.principal, thread_id, payload
                )
                for _ in range(contenders)
            ),
            return_exceptions=True,
        )
    refused = [result for result in results if isinstance(result, SelfWakeRefused)]
    assert len(refused) == contenders - (MAX_PENDING - already_pending)
    assert Counter(result.reason for result in refused) == {"too_many": len(refused)}
    assert not [
        result
        for result in results
        if isinstance(result, BaseException) and not isinstance(result, SelfWakeRefused)
    ]
    assert len(await service.pending(pi.principal)) == MAX_PENDING
    assert (
        len({result.id for result in results if not isinstance(result, BaseException)})
        == MAX_PENDING - already_pending
    )


@pytest.mark.parametrize("contenders", [2, 8])
async def test_concurrent_delivery_transactions_store_exactly_one_wake(
    settings: Settings, signer: "SignerFactory", monkeypatch: pytest.MonkeyPatch, contenders: int
) -> None:
    pi = signer("pi@testbox")
    async with _running_board(settings) as (app, client):
        app.state.registry.add_line(pi.allowed_line)
        thread_id = await _thread(client, pi)
    now = time.time()
    service = SelfWakes(Database(settings.db_path), clock=lambda: now)
    scheduled = await service.schedule(
        pi.principal,
        thread_id,
        {"at": datetime.fromtimestamp(now + 600, UTC).isoformat(), "note": "delivery contention review"},
    )
    with monkeypatch.context() as patch:
        _contend(patch, contenders)
        results = await asyncio.gather(
            *(
                Database(settings.db_path).transact(lambda conn: deliver(conn, scheduled.id, now + 601))
                for _ in range(contenders)
            )
        )
    assert Counter(results) == {pi.principal: 1, None: contenders - 1}
    with closing(sqlite3.connect(settings.db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM wakes WHERE source_ref = ?", (f"self_wake:{scheduled.id}",)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT fired_at, cancelled_at FROM self_wakes WHERE id = ?", (scheduled.id,)
        ).fetchone() == (now + 601, None)
    assert await service.pending(pi.principal) == []
