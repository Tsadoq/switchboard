import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_board.night import fixture_gate_store as store
from agent_board.night.fixture_gate_dto import encode_record

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_fixture_gate_store import (
        CONNECT,
        CONTEXT,
        OWNER,
        attestation,
        classification,
        committed,
    )
else:
    from test_night_fixture_gate_store import CONNECT, CONTEXT, OWNER, attestation, classification, committed

SCRIPT = """
import os
import sqlite3
import sys
from agent_board.night import fixture_gate_store as store
from agent_board.night.fixture_gate_dto import Context, RuntimeOwner, RuntimeSelector, decode_record
from pathlib import Path

context = CONTEXT_VALUE
owner = OWNER_VALUE
record = decode_record(bytes.fromhex(RECORD_VALUE), context=context, owner=owner)
path = Path(sys.argv[1])
action, statement, when = sys.argv[2:]
original_connect = sqlite3.connect
original_fsync = os.fsync
sync_calls = 0

def crash(sql, moment):
    if sql.startswith(statement) and when == moment:
        os._exit(73)

class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        crash(sql, 'before')
        result = super().execute(sql, parameters)
        crash(sql, 'after')
        return result

    def close(self):
        crash('close', 'before')
        super().close()
        crash('close', 'after')

def connect(*args, **kwargs):
    return original_connect(*args, **kwargs, factory=Connection)

def fsync(fd):
    global sync_calls
    sync_calls += 1
    name = f'fsync{sync_calls}'
    crash(name, 'before')
    original_fsync(fd)
    crash(name, 'after')

sqlite3.connect = connect
os.fsync = fsync
if action == 'provision':
    store.provision_fixture(path, context=context)
elif action == 'begin':
    store.begin_operation(path, context=context, owner=owner)
elif action == 'complete':
    store.complete_operation(path, context=context, attestation=record)
else:
    store.lock_fault(path, context=context, selector=RuntimeSelector(owner),
                     expected_state='PENDING', reason='RESOURCE_UNCERTAIN')
sys.exit(88)
"""

CASES = [
    (action, statement)
    for action, statements in (
        ("begin", ("INSERT INTO identities", "UPDATE control", "COMMIT", "close")),
        ("complete", ("INSERT INTO completions", "UPDATE identities", "UPDATE control", "COMMIT", "close")),
        ("fault", ("UPDATE identities", "UPDATE control", "COMMIT", "close")),
        (
            "provision",
            (
                "CREATE TABLE control",
                "CREATE TABLE identities",
                "CREATE TABLE completions",
                "PRAGMA user_version=1",
                "INSERT INTO identities",
                "INSERT INTO completions",
                "INSERT INTO control",
                "COMMIT",
                "close",
                "fsync1",
                "fsync2",
            ),
        ),
    )
    for statement in statements
]


def artifacts(path: Path) -> dict[str, bytes]:
    return {item.name: item.read_bytes() for item in path.parent.iterdir()}


def maintenance_control(path: Path) -> tuple[str, str, str, str | None]:
    with closing(CONNECT(path, isolation_level=None)) as conn:
        row = conn.execute("SELECT state,operation,launch,completion_ref FROM control").fetchone()
        assert row is not None
        return row[0], row[1], row[2], row[3]


@pytest.mark.parametrize("action,statement", CASES)
@pytest.mark.parametrize("when", ["before", "after"])
def test_actual_process_crash_never_publishes_a_partial_completion_or_implicitly_repairs(
    tmp_path: Path,
    action: str,
    statement: str,
    when: str,
) -> None:
    path = tmp_path / "gate.sqlite"
    if action != "provision":
        committed(store.provision_fixture(path, context=CONTEXT))
    if action in ("complete", "fault"):
        committed(store.begin_operation(path, context=CONTEXT, owner=OWNER))
    script = (
        SCRIPT.replace("CONTEXT_VALUE", repr(CONTEXT))
        .replace("OWNER_VALUE", repr(OWNER))
        .replace("RECORD_VALUE", repr(encode_record(attestation()).hex()))
    )
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(path), action, statement, when],
        capture_output=True,
        timeout=30,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert crashed.returncode == 73, (crashed.stdout, crashed.stderr, action, statement, when)
    assert crashed.stdout == b""
    before = artifacts(path)
    read = store.observe(path, context=CONTEXT)
    assert artifacts(path) == before
    after_commit = statement in ("close", "fsync2") or (statement == "COMMIT" and when == "after")
    if action == "provision" and not after_commit:
        assert read.observation is None
    elif any(name.endswith(("-journal", "-wal", "-shm")) for name in before):
        assert read.observation is None and read.fault == store.Fault.FILE_FAILURE
    else:
        assert read.observation is not None
        expected = {
            "begin": "PENDING" if after_commit else "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP",
            "complete": "IDLE_WITH_STRUCTURALLY_VALID_ATTESTATION" if after_commit else "PENDING",
            "fault": "FAULT_LOCKED" if after_commit else "PENDING",
            "provision": "IDLE_WITH_VALID_FIXTURE_BOOTSTRAP",
        }[action]
        assert read.observation.classification == expected
    assert store.provision_fixture(path, context=CONTEXT).refusal == store.Refusal.PREEXISTING_PATH
    if action == "provision" and not after_commit:
        return
    control = maintenance_control(path)
    expected_state = {
        "begin": "PENDING" if after_commit else "IDLE",
        "complete": "IDLE" if after_commit else "PENDING",
        "fault": "FAULT_LOCKED" if after_commit else "PENDING",
        "provision": "IDLE",
    }[action]
    assert control[0] == expected_state
    with closing(CONNECT(path, isolation_level=None)) as conn:
        identity = conn.execute(
            "SELECT status FROM identities WHERE operation=?",
            (control[1],),
        ).fetchone()
        rows = conn.execute(
            "SELECT ref,operation,launch FROM completions WHERE operation=? OR launch=?",
            (control[1], control[2]),
        ).fetchall()
    assert identity is not None
    if control[0] == "IDLE":
        assert identity[0] in ("BOOTSTRAP", "COMPLETED")
        assert len(rows) == 1 and rows[0] == (control[3], control[1], control[2])
    else:
        assert identity[0] == ("PENDING" if control[0] == "PENDING" else "FAULTED")
        assert control[3] is None and rows == []
    if action == "complete" and after_commit:
        assert classification(path) == "IDLE_WITH_STRUCTURALLY_VALID_ATTESTATION"
