import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

import pytest
from agent_board.night import ledger_arm as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome
from agent_board.night.ledger import Ledger

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path, put
else:
    from test_night_ledger_arm import ARM, CONNECT, JOURNAL, Tape, ledger_path, put

__all__ = ["ledger_path"]
OPEN, CLOSE, READ = os.open, os.close, os.read


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "empty",
        "short",
        "magic",
        "wal",
        "file_mode",
        "parent_mode",
        "symlink",
        "hardlink",
        "directory",
        "relative",
        "dotdot",
        "missing_parent",
        "symlink_parent",
        "unsafe_ancestor",
    ],
)
def test_real_private_file_boundary_refuses_without_sql_creation_or_repair(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    path = ledger_path
    if case == "missing":
        path.unlink()
    elif case == "empty":
        path.write_bytes(b"")
    elif case == "short":
        path.write_bytes(b"SQLite format 3\x00")
    elif case == "magic":
        path.write_bytes(b"x" * 100)
    elif case == "wal":
        data = bytearray(path.read_bytes())
        data[18:20] = b"\x02\x02"
        path.write_bytes(data)
    elif case == "file_mode":
        path.chmod(0o644)
    elif case == "parent_mode":
        path.parent.chmod(0o755)
    elif case == "symlink":
        path = path.with_name("link.sqlite")
        path.symlink_to(ledger_path)
    elif case == "hardlink":
        os.link(path, path.with_name("hardlink.sqlite"))
    elif case == "directory":
        path = path.parent
    elif case == "relative":
        path = path.relative_to(path.anchor)
    elif case == "dotdot":
        path = path.parent / ".." / path.parent.name / path.name
    elif case == "missing_parent":
        path = path.parent / "missing" / path.name
    elif case == "symlink_parent":
        alias = path.parent / "alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        path = alias / path.name
    elif case == "unsafe_ancestor":
        ancestor = path.parent / "unsafe"
        ancestor.mkdir(mode=0o700)
        nested = ancestor / "private"
        nested.mkdir(mode=0o700)
        destination = nested / path.name
        shutil.copyfile(path, destination)
        destination.chmod(0o600)
        ancestor.chmod(0o777)
        path = destination
    before = ledger_path.read_bytes() if ledger_path.is_file() else None
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert result.fault == writer.Fault.FILE_FAILURE and result.refusal is None and result.stored is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.cleanup == CleanupOutcome.CLOSED
    assert not tape.attempts and not tape.connections
    assert (ledger_path.read_bytes() if ledger_path.is_file() else None) == before
    if case in ("missing", "missing_parent"):
        assert not path.exists()
    ledger_path.parent.chmod(0o700)


def test_actual_world_writable_tmp_ancestry_refused_even_with_private_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory(prefix="la.unsafe.", dir="/tmp") as directory:
        path = Path(directory) / "ledger.sqlite"
        path.write_bytes(b"x" * 100)
        path.chmod(0o600)
        tape = Tape(monkeypatch)
        result = writer.compare_and_set_arm(path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert result.fault == writer.Fault.FILE_FAILURE and result.refusal is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and not tape.connections


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_any_retained_sidecar_path_is_presence_only_and_never_removed_or_opened(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    kind: str,
) -> None:
    sidecar = ledger_path.with_name(ledger_path.name + suffix)
    if kind == "file":
        sidecar.write_bytes(b"retained opaque evidence")
    elif kind == "directory":
        sidecar.mkdir()
    else:
        sidecar.symlink_to(sidecar.with_name("missing-target"))
    inode = sidecar.lstat().st_ino
    before = ledger_path.read_bytes()
    tape = Tape(monkeypatch)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert result.refusal == writer.Refusal.SIDECAR_PRESENT and result.fault is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.cleanup == CleanupOutcome.CLOSED
    assert result.stored is None and not tape.connections and not tape.attempts
    assert sidecar.lstat().st_ino == inode and ledger_path.read_bytes() == before


@pytest.mark.parametrize("header_bad", [False, True])
@pytest.mark.parametrize("close_when", ["before", "after"])
def test_probe_close_failure_is_independent_and_outweighs_header_failure(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    header_bad: bool,
    close_when: str,
) -> None:
    if header_bad:
        ledger_path.write_bytes(b"x" * 100)
    owned: list[int] = []
    closed: list[int] = []

    def opened(*args: Any, **kwargs: Any) -> int:
        fd = OPEN(*args, **kwargs)
        if args[0] == ledger_path:
            owned.append(fd)
        return fd

    def close(fd: int) -> None:
        if fd in owned:
            closed.append(fd)
            if close_when == "after":
                CLOSE(fd)
                owned.remove(fd)
            raise OSError("injected probe close failure")
        CLOSE(fd)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", close)
    tape = Tape(monkeypatch)
    try:
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert len(closed) == 1 and len(owned) == (1 if close_when == "before" else 0)
        assert not tape.connections
        assert result.fault == writer.Fault.CLOSE_FAILURE and result.refusal is None
        assert result.cleanup == CleanupOutcome.UNCERTAIN and result.mutation == MutationOutcome.NOT_ATTEMPTED
        assert result.stored is None
    finally:
        if close_when == "before":
            for fd in owned:
                CLOSE(fd)


@pytest.mark.parametrize("stage", ["probe_open", "probe_read", "connect"])
def test_actual_resource_boundary_failures_do_not_enter_body_or_leak_owned_probe(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    closed: list[int] = []
    live: set[int] = set()

    def opened(*args: Any, **kwargs: Any) -> int:
        if args[0] == ledger_path and stage == "probe_open":
            raise OSError("injected probe open failure")
        fd = OPEN(*args, **kwargs)
        if args[0] == ledger_path:
            live.add(fd)
        return fd

    def read(fd: int, count: int) -> bytes:
        if fd in live:
            raise OSError("injected probe read failure")
        return READ(fd, count)

    def close(fd: int) -> None:
        if fd in live:
            closed.append(fd)
            live.remove(fd)
        CLOSE(fd)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        raise sqlite3.OperationalError("not a typed busy error")

    monkeypatch.setattr(os, "open", opened)
    if stage == "probe_read":
        monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(sqlite3, "connect", connect)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert result.fault == (writer.Fault.SQLITE_FAILURE if stage == "connect" else writer.Fault.FILE_FAILURE)
    assert (
        result.refusal is None and result.stored is None and result.mutation == MutationOutcome.NOT_ATTEMPTED
    )
    assert result.cleanup == CleanupOutcome.CLOSED and len(closed) == (0 if stage == "probe_open" else 1)
    assert not live
    other = ledger_path.with_name("unrelated")
    other.write_bytes(b"unrelated")
    fd = os.open(other, os.O_RDONLY)
    try:
        assert os.read(fd, 9) == b"unrelated"
    finally:
        os.close(fd)
    assert len(closed) == (0 if stage == "probe_open" else 1)


@pytest.mark.parametrize("boundary", ["probe_open", "connect"])
def test_real_inode_replacement_at_probe_or_after_sqlite_open_refuses_without_sql(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    original = ledger_path.stat().st_ino
    old = ledger_path.with_name("retained.sqlite")
    connections: list[sqlite3.Connection] = []

    def replace_file() -> None:
        ledger_path.rename(old)
        shutil.copyfile(old, ledger_path)
        ledger_path.chmod(0o600)

    def opened(*args: Any, **kwargs: Any) -> int:
        if args[0] == ledger_path:
            replace_file()
        return OPEN(*args, **kwargs)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = CONNECT(*args, **kwargs)
        connections.append(conn)
        replace_file()
        return conn

    if boundary == "probe_open":
        monkeypatch.setattr(os, "open", opened)
    else:
        monkeypatch.setattr(sqlite3, "connect", connect)
    result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
    assert old.stat().st_ino == original != ledger_path.stat().st_ino
    assert result.fault == writer.Fault.FILE_FAILURE and result.refusal is None
    assert result.cleanup == CleanupOutcome.CLOSED and result.mutation == MutationOutcome.NOT_ATTEMPTED
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_new_file_success_does_not_dispose_old_live_snapshot_or_authenticate_it(ledger_path: Path) -> None:
    put(ledger_path, ARM)
    with closing(CONNECT(ledger_path, isolation_level=None)) as old:
        old.execute("BEGIN")
        assert old.execute("SELECT * FROM arms").fetchall()
        identity = ledger_path.stat().st_ino
        ledger_path.rename(ledger_path.with_name("old.sqlite"))
        Ledger(ledger_path)
        with closing(CONNECT(ledger_path, isolation_level=None)) as setup:
            setup.execute("UPDATE identity SET journal_id=?", (JOURNAL,))
        result = writer.compare_and_set_arm(ledger_path, journal_id=JOURNAL, desired=ARM, expected_prior=None)
        assert result.mutation == MutationOutcome.COMMITTED and result.changed is True
        assert ledger_path.stat().st_ino != identity and old.in_transaction
        assert old.execute("SELECT run_id FROM arms").fetchall() == [(ARM.run_id,)]
        old.execute("ROLLBACK")
