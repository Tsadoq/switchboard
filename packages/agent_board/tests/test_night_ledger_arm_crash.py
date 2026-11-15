import json
import subprocess
import sys
from contextlib import closing
from dataclasses import astuple, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import ledger_arm as writer
from agent_board.night.journal_latch import MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path, put
else:
    from test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path, put

__all__ = ["ledger_path"]

SCRIPT = """
import json, os, sqlite3, sys
from pathlib import Path
from agent_board.night import ledger_arm as writer
from agent_board.night.ledger import Arm
path, branch, point, when, journal = sys.argv[1:]
arm = Arm('20000000-0000-4000-8000-000000000000', '30000000-0000-4000-8000-000000000000', 'a'*32, 1000.0, 2000.0)
desired = arm if branch == 'noop' else Arm(arm.run_id, arm.guard_boot_id, 'b'*32, 1000.0, 2000.0)
native = []
original = sqlite3.connect

def crash():
    print(json.dumps({'point': point, 'when': when, 'native': native}), flush=True)
    os._exit(77)

class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        selected = (point == 'dml' and sql.startswith(('INSERT INTO arms', 'UPDATE arms'))) or sql == point
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
writer.compare_and_set_arm(Path(path), journal_id=journal, desired=desired, expected_prior=None if branch == 'insert' else arm)
raise RuntimeError('crash point was not reached')
"""

CASES = [
    (branch, point, when)
    for branch in ("insert", "replace")
    for point in ("dml", "COMMIT", "close")
    for when in ("before", "after")
]
CASES += [("noop", point, when) for point in ("ROLLBACK", "close") for when in ("before", "after")]


@pytest.mark.parametrize("branch,point,when", CASES)
def test_real_interpreter_exit_around_dml_commit_rollback_close_and_lost_reply(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    point: str,
    when: str,
) -> None:
    prior = ARM if branch != "insert" else None
    if prior is not None:
        put(ledger_path, prior)
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
        assert any(sql.startswith(("INSERT INTO arms", "UPDATE arms")) for sql in native) is (when == "after")
        assert "COMMIT" not in native
    if point == "COMMIT":
        assert ("COMMIT" in native) is (when == "after")
    if point == "close":
        assert native[-1] == ("ROLLBACK" if branch == "noop" else "COMMIT")
    sidecar = ledger_path.with_name(ledger_path.name + "-journal")
    if sidecar.exists():
        main_before, journal_before = ledger_path.read_bytes(), sidecar.read_bytes()
        tape = Tape(monkeypatch)
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert (
            result.refusal == writer.Refusal.SIDECAR_PRESENT
            and result.mutation == MutationOutcome.NOT_ATTEMPTED
        )
        assert result.fault is None and result.stored is None and not tape.attempts and not tape.connections
        assert ledger_path.read_bytes() == main_before and sidecar.read_bytes() == journal_before
    durable = (
        replace(ARM, guard_invocation_id="b" * 32)
        if branch != "noop" and (point == "close" or (point == "COMMIT" and when == "after"))
        else prior
    )
    with closing(CONNECT(ledger_path, isolation_level=None)) as fixture_maintenance:
        observed = fixture_maintenance.execute("SELECT * FROM arms").fetchall()
        assert observed == ([] if durable is None else [astuple(durable)])
        assert fixture_maintenance.execute("SELECT journal_id,acked_counter FROM identity").fetchall() == [
            (JOURNAL, 0)
        ]
