import os
import shutil
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from agent_board.night import capture_queue as queue
from agent_board.night.journal_latch import CleanupOutcome, MutationOutcome

if TYPE_CHECKING:
    from packages.agent_board.tests.test_night_capture_queue import PUBLICATION
    from packages.agent_board.tests.test_night_effective_queue import EMPTY, queue_path
    from packages.agent_board.tests.test_night_ledger_arm import Tape
else:
    from test_night_capture_queue import PUBLICATION
    from test_night_effective_queue import EMPTY, queue_path
    from test_night_ledger_arm import Tape

__all__ = ["queue_path"]


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
    ],
)
def test_real_unsafe_files_and_ancestry_fail_without_sql_or_runtime_creation(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    path: Any = queue_path
    before = queue_path.read_bytes()
    if case in ("missing", "symlink", "directory", "fifo"):
        queue_path.unlink()
    if case == "symlink":
        other = queue_path.with_name("real.sqlite")
        other.write_bytes(before)
        queue_path.symlink_to(other)
    elif case == "hardlink":
        os.link(queue_path, queue_path.with_name("alias.sqlite"))
    elif case == "directory":
        queue_path.mkdir()
    elif case == "fifo":
        os.mkfifo(queue_path)
    elif case == "short":
        queue_path.write_bytes(before[:99])
    elif case == "magic":
        queue_path.write_bytes(b"NotSQLiteFormat!" + before[16:])
    elif case == "wal-header":
        queue_path.write_bytes(before[:18] + b"\x02\x02" + before[20:])
    elif case == "mode":
        queue_path.chmod(0o666)
    elif case == "ancestry":
        queue_path.parent.chmod(0o777)
    elif case == "relative":
        path = Path("ledger.sqlite")
    tape = Tape(monkeypatch)
    try:
        result = queue.publish_capture(path, expected=EMPTY, publication=PUBLICATION)
        assert result.fault == queue.Fault.FILE_FAILURE and result.refusal is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
        assert result.cleanup == CleanupOutcome.CLOSED and result.control is None
        assert not tape.connections and not tape.attempts
        if case == "missing":
            assert not queue_path.exists()
    finally:
        queue_path.parent.chmod(0o700)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("kind", ["file", "directory", "dangling-symlink"])
def test_sidecar_presence_has_no_hotness_or_liveness_interpretation_and_changes_nothing(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    kind: str,
) -> None:
    sidecar = queue_path.with_name(queue_path.name + suffix)
    if kind == "file":
        sidecar.write_bytes(b"opaque retained sidecar")
    elif kind == "directory":
        sidecar.mkdir()
    else:
        sidecar.symlink_to(queue_path.with_name("absent"))
    before, metadata = queue_path.read_bytes(), sidecar.lstat()
    tape = Tape(monkeypatch)
    result = queue.publish_capture(queue_path, expected=EMPTY, publication=PUBLICATION)
    assert result.refusal == queue.Refusal.SIDECAR_PRESENT and result.fault is None
    assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.control is None
    assert not tape.connections and not tape.attempts
    assert queue_path.read_bytes() == before and sidecar.lstat() == metadata


@pytest.mark.parametrize("when", ["before-open", "after-open"])
@pytest.mark.parametrize("close_fault", [None, "before", "after"])
def test_actual_inode_replacement_across_connection_open_refuses_before_sql(
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    close_fault: str | None,
) -> None:
    replacement, retained = queue_path.with_name("replacement.sqlite"), queue_path.with_name("old.sqlite")
    shutil.copy2(queue_path, replacement)
    initial = queue_path.stat().st_ino
    tape = Tape(monkeypatch, close_fault=close_fault)
    opened = sqlite3.connect

    def swap() -> None:
        queue_path.rename(retained)
        replacement.rename(queue_path)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if when == "before-open":
            swap()
        conn = cast(sqlite3.Connection, opened(*args, **kwargs))
        if when == "after-open":
            swap()
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        result = queue.publish_capture(queue_path, expected=EMPTY, publication=PUBLICATION)
        assert queue_path.stat().st_ino != initial and retained.stat().st_ino == initial
        assert result.fault == (queue.Fault.CLOSE_FAILURE if close_fault else queue.Fault.FILE_FAILURE)
        assert result.refusal is None and result.control is None
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
    queue_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
    close_fault: str | None,
) -> None:
    if probe == "header":
        queue_path.write_bytes(b"bad header" * 10)
    real_open, real_close, real_read = os.open, os.close, os.read
    live: set[int] = set()
    opened, closed = 0, 0

    def open_fd(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal opened
        fd = real_open(path, *args, **kwargs)
        if path == queue_path:
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
        result = queue.publish_capture(queue_path, expected=EMPTY, publication=PUBLICATION)
        assert result.fault == (queue.Fault.CLOSE_FAILURE if close_fault else queue.Fault.FILE_FAILURE)
        assert result.refusal is None and result.control is None
        assert result.mutation == MutationOutcome.NOT_ATTEMPTED and result.changed is False
        assert result.cleanup == (CleanupOutcome.UNCERTAIN if close_fault else CleanupOutcome.CLOSED)
        assert opened == closed == 1 and len(live) == int(close_fault == "before")
        assert not tape.connections and not tape.attempts
    finally:
        for fd in live:
            real_close(fd)
