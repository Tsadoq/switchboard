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


@pytest.mark.parametrize("code", [None, True, "5", 5, 6, 261, 262])
@pytest.mark.parametrize("phase", ["connect", 0, 3, 4, 11])
def test_only_exact_typed_busy_locked_before_successful_begin_is_a_refusal(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: Any,
    phase: str | int,
) -> None:
    attempts: list[str] = []
    native: list[str] = []
    skipped: list[int] = []
    connections: list[sqlite3.Connection] = []
    closes = 0

    def error() -> sqlite3.Error:
        result = sqlite3.OperationalError("database is locked")
        if code is not None:
            result.sqlite_errorcode = code
        return result

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            index = len(attempts)
            attempts.append(sql)
            if index == phase:
                skipped.append(index)
                raise error()
            return super().execute(sql, parameters)

        def close(self) -> None:
            nonlocal closes
            closes += 1
            super().close()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        assert kwargs == {"uri": True, "isolation_level": None, "timeout": 0}
        if phase == "connect":
            raise error()
        conn = CONNECT(*args, **kwargs, factory=Connection)
        conn.set_trace_callback(native.append)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        busy = type(code) is int and code & 255 in (5, 6) and phase in ("connect", 0, 3)
        assert result.refusal == (writer.Refusal.BUSY if busy else None)
        assert result.fault == (None if busy else writer.Fault.SQLITE_FAILURE)
        assert result.mutation == (
            MutationOutcome.UNKNOWN
            if phase == 11
            else MutationOutcome.ROLLED_BACK
            if phase == 4
            else MutationOutcome.NOT_ATTEMPTED
        )
        assert result.changed is (None if phase == 11 else False)
        assert result.cleanup == CleanupOutcome.CLOSED and result.stored is None
        assert closes == len(connections) == (0 if phase == "connect" else 1)
        assert len(native) + len(skipped) == len(attempts)
        assert attempts.count("ROLLBACK") == int(phase in (4, 11))
        if phase == 11:
            assert len(attempts) == 13 and attempts[-2:] == ["COMMIT", "ROLLBACK"]
        for conn in connections:
            with pytest.raises(sqlite3.ProgrammingError):
                sqlite3.Connection.execute(conn, "SELECT 1")
    finally:
        for conn in connections:
            sqlite3.Connection.close(conn)
    assert read(ledger_path) == []
