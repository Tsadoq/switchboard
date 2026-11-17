import json
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import effective_queue as queue
from agent_board.night.journal_latch import MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_effective_queue import CONNECT, EMPTY, queue_path, tables
    from packages.agent_board.tests.test_night_effective_queue_counts import TRANSITIONS
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_effective_queue import CONNECT, EMPTY, queue_path, tables
    from test_night_effective_queue_counts import TRANSITIONS
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]
TESTS = Path(__file__).parent

SCRIPT = """
import json, os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_board.night import effective_queue as queue
from test_night_effective_queue_counts import TRANSITIONS
path, name, point, when, control = sys.argv[2:]
operation = next(op for n, _, _, op in TRANSITIONS if n == name)
native = []
original = sqlite3.connect
seen = {'dml': 0}

def crash():
    print(json.dumps({'native': native}), flush=True)
    os._exit(77)

def selected(sql):
    if point == 'first-dml' and sql.startswith(('INSERT', 'UPDATE')):
        seen['dml'] += 1
        return seen['dml'] == 1
    if point == 'last-dml':
        return sql.startswith('UPDATE effective_queue')
    return sql == point

class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        hit = selected(sql)
        if hit and when == 'before':
            crash()
        cursor = super().execute(sql, parameters)
        if hit and when == 'after':
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
operation(Path(path), queue.ExpectedControl(**json.loads(control)))
raise RuntimeError('crash point was not reached')
"""

MUTATING = [
    (name, prepare)
    for name, _, prepare, _ in TRANSITIONS
    if name.startswith(("publish-binding", "publish-evidence", "acknowledge"))
]
CASES = [
    pytest.param(name, prepare, point, when, id=f"{name}-{point}-{when}")
    for name, prepare in MUTATING
    for point in ("first-dml", "last-dml", "COMMIT", "close")
    for when in ("before", "after")
]


@pytest.mark.parametrize(("name", "prepare", "point", "when"), CASES)
def test_an_interpreter_exit_returns_nothing_and_a_retained_journal_refuses_with_zero_sql(
    queue_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, prepare: object, point: str, when: str
) -> None:
    control = prepare(queue_path)  # type: ignore[operator]
    before = tables(queue_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            SCRIPT,
            str(TESTS),
            str(queue_path),
            name,
            point,
            when,
            json.dumps(asdict(control)),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 77, completed.stderr
    native = json.loads(completed.stdout)["native"]
    assert native.count("BEGIN IMMEDIATE") == 1
    committed = point == "close" or (point == "COMMIT" and when == "after")
    assert ("COMMIT" in native) is committed

    sidecar = queue_path.with_name(queue_path.name + "-journal")
    wrote = any(sql.startswith(("INSERT", "UPDATE")) for sql in native)
    assert sidecar.exists() is (wrote and not committed)
    if sidecar.exists():
        main_before, journal_before = queue_path.read_bytes(), sidecar.read_bytes()
        tape = Tape(monkeypatch)
        for result in (
            queue.observe_ordinary(queue_path, expected=control),
            queue.observe_ordinary(queue_path, expected=EMPTY),
        ):
            assert result.refusal == queue.Refusal.SIDECAR_PRESENT and result.fault is None
            assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.control is None
        assert not tape.attempts and not tape.connections
        assert queue_path.read_bytes() == main_before and sidecar.read_bytes() == journal_before
        monkeypatch.undo()

    after = tables(queue_path)
    assert (after != before) is committed
    with closing(CONNECT(queue_path, isolation_level=None)) as fixture_maintenance:
        revision = fixture_maintenance.execute("SELECT revision FROM effective_queue").fetchone()[0]
    assert revision == control.revision + int(committed)
