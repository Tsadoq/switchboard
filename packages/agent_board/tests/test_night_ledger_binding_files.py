import os
import shutil
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from agent_board.night import ledger_binding as writer
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_ledger_arm import JOURNAL, Tape
    from packages.agent_board.tests.test_night_ledger_binding import CLAIM, ledger_path
else:
    from test_night_ledger_arm import JOURNAL, Tape
    from test_night_ledger_binding import CLAIM, ledger_path

__all__ = ["ledger_path"]


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "symlink",
        "hardlink",
        "directory",
        "fifo",
        "short",
        "magic",
        "wal-header",
        "mode",
        "ancestry",
        "relative",
        "string",
    ],
)
def test_real_unsafe_files_and_ancestry_fail_without_sql_or_runtime_creation(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    path: Any = ledger_path
    before = ledger_path.read_bytes()
    if case in ("missing", "symlink", "directory", "fifo"):
        ledger_path.unlink()
    if case == "symlink":
        other = ledger_path.with_name("real.sqlite")
        other.write_bytes(before)
        ledger_path.symlink_to(other)
    elif case == "hardlink":
        os.link(ledger_path, ledger_path.with_name("alias.sqlite"))
    elif case == "directory":
        ledger_path.mkdir()
    elif case == "fifo":
        os.mkfifo(ledger_path)
    elif case == "short":
        ledger_path.write_bytes(before[:99])
    elif case == "magic":
        ledger_path.write_bytes(b"NotSQLiteFormat!" + before[16:])
    elif case == "wal-header":
        ledger_path.write_bytes(before[:18] + b"\x02\x02" + before[20:])
    elif case == "mode":
        ledger_path.chmod(0o666)
    elif case == "ancestry":
        ledger_path.parent.chmod(0o777)
    elif case == "relative":
        path = Path("ledger.sqlite")
    elif case == "string":
        path = str(ledger_path)
    tape = Tape(monkeypatch)
    try:
        result = writer.publish_binding(path, journal_id=JOURNAL, claim=CLAIM)
        assert result.fault == writer.Fault.FILE_FAILURE and result.refusal is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
        assert result.cleanup == CleanupOutcome.CLOSED and result.stored is None
        assert not tape.connections and not tape.attempts
        if case == "missing":
            assert not ledger_path.exists()
    finally:
        ledger_path.parent.chmod(0o700)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("kind", ["file", "directory", "dangling-symlink"])
def test_sidecar_presence_has_no_hotness_or_liveness_interpretation_and_changes_nothing(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    kind: str,
) -> None:
    sidecar = ledger_path.with_name(ledger_path.name + suffix)
    if kind == "file":
        sidecar.write_bytes(b"opaque retained sidecar")
    elif kind == "directory":
        sidecar.mkdir()
    else:
        sidecar.symlink_to(ledger_path.with_name("absent"))
    before, metadata = ledger_path.read_bytes(), sidecar.lstat()
    tape = Tape(monkeypatch)
    result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
    assert result.refusal == writer.Refusal.SIDECAR_PRESENT and result.fault is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.stored is None
    assert not tape.connections and not tape.attempts
    assert ledger_path.read_bytes() == before and sidecar.lstat() == metadata


@pytest.mark.parametrize("when", ["before-open", "after-open"])
@pytest.mark.parametrize("close_fault", [None, "before", "after"])
def test_actual_inode_replacement_across_connection_open_refuses_before_sql(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    close_fault: str | None,
) -> None:
    replacement, retained = ledger_path.with_name("replacement.sqlite"), ledger_path.with_name("old.sqlite")
    shutil.copy2(ledger_path, replacement)
    initial = ledger_path.stat().st_ino
    tape = Tape(monkeypatch, close_fault=close_fault)
    opened = sqlite3.connect

    def swap() -> None:
        ledger_path.rename(retained)
        replacement.rename(ledger_path)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if when == "before-open":
            swap()
        conn = cast(sqlite3.Connection, opened(*args, **kwargs))
        if when == "after-open":
            swap()
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert ledger_path.stat().st_ino != initial and retained.stat().st_ino == initial
        assert result.fault == (writer.Fault.CLOSE_FAILURE if close_fault else writer.Fault.FILE_FAILURE)
        assert result.refusal is None and result.stored is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED
        assert result.cleanup == (CleanupOutcome.UNCERTAIN if close_fault else CleanupOutcome.CLOSED)
        assert not tape.attempts and tape.closes == 1
        tape.reconcile()
    finally:
        tape.dispose()


@pytest.mark.parametrize(
    "probe,close_fault",
    [
        (probe, close)
        for probe in ("normal", "read-before", "read-after", "header")
        for close in (None, "before", "after")
        if probe != "normal" or close is not None
    ],
)
def test_owned_probe_fd_cleanup_faults_are_independent_of_trigger_and_live_resource_count(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
    close_fault: str | None,
) -> None:
    if probe == "header":
        ledger_path.write_bytes(b"bad header" * 10)
    real_open, real_close, real_read = os.open, os.close, os.read
    live: set[int] = set()
    opened, closed = 0, 0

    def open_fd(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal opened
        fd = real_open(path, *args, **kwargs)
        if path == ledger_path:
            live.add(fd)
            opened += 1
        return fd

    def close_fd(fd: int) -> None:
        nonlocal closed
        if fd not in live:
            real_close(fd)
            return
        closed += 1
        if close_fault == "before":
            raise OSError("probe close before native")
        real_close(fd)
        live.remove(fd)
        if close_fault == "after":
            raise OSError("probe close after native")

    def read_fd(fd: int, amount: int) -> bytes:
        if fd in live and probe == "read-before":
            raise OSError("probe read before native")
        value = real_read(fd, amount)
        if fd in live and probe == "read-after":
            raise OSError("probe read after native")
        return value

    monkeypatch.setattr(os, "open", open_fd)
    monkeypatch.setattr(os, "close", close_fd)
    monkeypatch.setattr(os, "read", read_fd)
    tape = Tape(monkeypatch)
    try:
        result = writer.publish_binding(ledger_path, journal_id=JOURNAL, claim=CLAIM)
        assert result.fault == (writer.Fault.CLOSE_FAILURE if close_fault else writer.Fault.FILE_FAILURE)
        assert result.refusal is None and result.stored is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
        assert result.cleanup == (CleanupOutcome.UNCERTAIN if close_fault else CleanupOutcome.CLOSED)
        assert opened == closed == 1 and len(live) == int(close_fault == "before")
        assert not tape.connections and not tape.attempts
    finally:
        for fd in live:
            real_close(fd)
