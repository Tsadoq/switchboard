import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import ledger_binding as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import CONNECT, JOURNAL
    from packages.agent_board.tests.test_night_ledger_binding import CLAIM, ledger_path, read
else:
    from test_night_ledger_arm import CONNECT, JOURNAL
    from test_night_ledger_binding import CLAIM, ledger_path, read

__all__ = ["ledger_path"]


@pytest.mark.parametrize(
    "case",
    [
        "rowcount0",
        "rowcount2",
        "rowcount-1",
        "empty",
        "duplicate",
        "bad_uid",
        "other_uid",
        "ack1",
        "ack2",
        "bad_inv",
    ],
)
@pytest.mark.parametrize("rollback", [None, "before", "after"])
@pytest.mark.parametrize("close", [None, "before", "after"])
def test_native_insert_then_impossible_result_is_snapshot_fault_never_a_semantic_refusal(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    rollback: str | None,
    close: str | None,
) -> None:
    attempts: list[str] = []
    native: list[str] = []
    skipped: list[int] = []
    connections: list[sqlite3.Connection] = []
    target_reads, closes = 0, 0

    class Cursor(sqlite3.Cursor):
        statement: str = ""

        @property
        def rowcount(self) -> int:
            if self.statement.startswith("INSERT") and case.startswith("rowcount"):
                return int(case.removeprefix("rowcount"))
            return super().rowcount

        def fetchall(self) -> list[Any]:
            nonlocal target_reads
            actual = super().fetchall()
            if self.statement != writer.BINDING_QUERY:
                return actual
            target_reads += 1
            if target_reads != 2 or case.startswith("rowcount"):
                return actual
            assert len(actual) == 1
            if case == "empty":
                return []
            if case == "duplicate":
                return actual * 2
            row = dict(actual[0])
            if case == "bad_uid":
                row["uid"] = 0
            elif case == "other_uid":
                row["uid"] = 1001
            elif case in ("ack1", "ack2"):
                row["server_acked"] = int(case[-1])
            else:
                row["invocation_id"] = b"0" * 32
            return [row]

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            index = len(attempts)
            attempts.append(sql)
            if sql == "ROLLBACK" and rollback == "before":
                skipped.append(index)
                raise sqlite3.OperationalError("injected rollback before native")
            cursor = self.cursor(factory=Cursor)
            cursor.statement = sql
            cursor.execute(sql, parameters)
            if sql == "ROLLBACK" and rollback == "after":
                raise sqlite3.OperationalError("injected rollback after native")
            return cursor

        def close(self) -> None:
            nonlocal closes
            closes += 1
            if close == "before":
                raise sqlite3.OperationalError("injected close before native")
            super().close()
            if close == "after":
                raise sqlite3.OperationalError("injected close after native")

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        assert kwargs == {"uri": True, "isolation_level": None, "timeout": 0}
        conn = CONNECT(*args, **kwargs, factory=Connection)
        conn.set_trace_callback(native.append)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert result.refusal is None and result.stored is None
        expected = (
            writer.Fault.CLOSE_FAILURE
            if close
            else writer.Fault.ROLLBACK_FAILURE
            if rollback
            else writer.Fault.SNAPSHOT_FAILURE
        )
        assert result.fault == expected
        assert result.cleanup == (CleanupOutcome.UNCERTAIN if close else CleanupOutcome.CLOSED)
        uncertain = bool(close and rollback)
        assert result.mutation == (MutationOutcome.UNKNOWN if uncertain else MutationOutcome.ROLLED_BACK)
        assert result.changed is (None if uncertain else False)
        assert "COMMIT" not in attempts and attempts.count("ROLLBACK") == closes == 1
        assert len(attempts) == (11 if case.startswith("rowcount") else 12)
        assert target_reads == (1 if case.startswith("rowcount") else 2)
        assert len(native) + len(skipped) == len(attempts)
    finally:
        for conn in connections:
            sqlite3.Connection.close(conn)
    assert read(ledger_path) == []
