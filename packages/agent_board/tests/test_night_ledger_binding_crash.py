import json
import subprocess
import sys
from contextlib import closing
from dataclasses import astuple
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import ledger_binding as writer
from agent_board.night.journal_latch import MutationOutcome
from agent_board.night.ledger import Ledger

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import CONNECT, JOURNAL, Tape
    from packages.agent_board.tests.test_night_ledger_binding import CLAIM, ledger_path, put
else:
    from test_night_ledger_arm import CONNECT, JOURNAL, Tape
    from test_night_ledger_binding import CLAIM, ledger_path, put

__all__ = ["ledger_path"]

SCRIPT = """
import json, os, sqlite3, sys
from pathlib import Path
from agent_board.night import ledger_binding as writer
from agent_board.night_inspection import unit_cgroup
path, branch, point, when, journal = sys.argv[1:]
run = '20000000-0000-4000-8000-000000000000'
claim = writer.BindingClaim(run, 1001 if branch == 'conflict1' else 1000, '30000000-0000-4000-8000-000000000000', 'a'*32, unit_cgroup(f'switchboard-night-{run}.service'))
native = []
original = sqlite3.connect

def crash():
    print(json.dumps({'point': point, 'when': when, 'native': native}), flush=True)
    os._exit(77)

class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        selected = (point == 'dml' and sql.startswith('INSERT INTO bindings')) or sql == point
        if selected and when == 'before':
            crash()
        cursor = super().execute(sql, parameters)
        if selected and when == 'after':
            crash()
        return cursor
    def close(self):
        if point == 'close' and when == 'before':
            crash()
        super().close()
        if point == 'close' and when == 'after':
            crash()

def connect(*args, **kwargs):
    conn = original(*args, **kwargs, factory=Connection)
    conn.set_trace_callback(native.append)
    return conn
sqlite3.connect = connect
writer.publish_binding(Path(path), journal_id=journal, claim=claim)
raise RuntimeError('crash point was not reached')
"""

CASES = [("insert", point, when) for point in ("dml", "COMMIT", "close") for when in ("before", "after")]
CASES += [
    (branch, point, when)
    for branch in ("match0", "match1", "conflict1")
    for point in ("ROLLBACK", "close")
    for when in ("before", "after")
]


@pytest.mark.parametrize("branch,point,when", CASES)
def test_actual_interpreter_exit_has_no_response_and_sidecar_refusal_is_not_recovery(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    point: str,
    when: str,
) -> None:
    ack = int(branch.endswith("1"))
    if branch != "insert":
        put(ledger_path, ack=ack)
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(ledger_path), branch, point, when, JOURNAL],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 77, completed.stderr
    witness = json.loads(completed.stdout)
    assert witness["point"] == point and witness["when"] == when
    native = witness["native"]
    assert native.count("BEGIN IMMEDIATE") == 1
    if point == "dml":
        assert len(native) == (9 if when == "before" else 10)
        assert any(sql.startswith("INSERT INTO bindings") for sql in native) is (when == "after")
        assert "COMMIT" not in native
    elif point == "COMMIT":
        assert len(native) == (11 if when == "before" else 12)
        assert ("COMMIT" in native) is (when == "after")
    elif point == "ROLLBACK":
        assert len(native) == (9 if when == "before" else 10)
        assert ("ROLLBACK" in native) is (when == "after")
    else:
        assert len(native) == (12 if branch == "insert" else 10)
        assert native[-1] == ("COMMIT" if branch == "insert" else "ROLLBACK")
    if branch != "insert":
        assert "COMMIT" not in native
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in native)
    sidecar = ledger_path.with_name(ledger_path.name + "-journal")
    if sidecar.exists():
        main_before, journal_before = ledger_path.read_bytes(), sidecar.read_bytes()
        tape = Tape(monkeypatch)
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert result.refusal == writer.Refusal.SIDECAR_PRESENT and result.fault is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.stored is None
        assert not tape.attempts and not tape.connections
        assert ledger_path.read_bytes() == main_before and sidecar.read_bytes() == journal_before
    durable = branch != "insert" or point == "close" or (point == "COMMIT" and when == "after")
    with closing(CONNECT(ledger_path, isolation_level=None)) as fixture_maintenance:
        assert fixture_maintenance.execute("SELECT * FROM bindings").fetchall() == (
            [(*astuple(CLAIM), ack)] if durable else []
        )
        assert fixture_maintenance.execute("SELECT journal_id,acked_counter FROM identity").fetchall() == [
            (JOURNAL, 0)
        ]


def test_new_path_success_does_not_close_or_invalidate_renamed_file_live_snapshot(ledger_path: Path) -> None:
    put(ledger_path, ack=1)
    with closing(CONNECT(ledger_path, isolation_level=None)) as old:
        old.execute("BEGIN")
        expected = [(*astuple(CLAIM), 1)]
        assert old.execute("SELECT * FROM bindings").fetchall() == expected
        old_inode = ledger_path.stat().st_ino
        renamed = ledger_path.with_name("retained-old.sqlite")
        ledger_path.rename(renamed)
        Ledger(ledger_path)
        with closing(CONNECT(ledger_path, isolation_level=None)) as fixture:
            fixture.execute("UPDATE identity SET journal_id=?", (JOURNAL,))
        assert ledger_path.stat().st_ino != old_inode and renamed.stat().st_ino == old_inode
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert result.mutation == MutationOutcome.COMMITTED and result.stored == writer.StoredBinding(
            JOURNAL, CLAIM, False
        )
        assert old.in_transaction and old.execute("SELECT * FROM bindings").fetchall() == expected
        with closing(CONNECT(ledger_path)) as new:
            assert new.execute("SELECT * FROM bindings").fetchall() == [(*astuple(CLAIM), 0)]
        old.execute("ROLLBACK")
