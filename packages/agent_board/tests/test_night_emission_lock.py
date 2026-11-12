import fcntl
import multiprocessing
import os
import sys
import threading
from collections.abc import Iterator
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest
from agent_board import night_state
from agent_board.night.emission_lock import EmissionLock, EmissionLockBusy, EmissionLockError


@pytest.fixture(autouse=True)
def protected_fixture_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def metadata(path: Path) -> os.stat_result:
        result = path.lstat()
        if path in tmp_path.parents:
            values = list(result)
            values[0], values[4] = 0o40755, 0
            return os.stat_result(values)
        return result

    monkeypatch.setattr(night_state, "_ancestor_stat", metadata)


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    path = tmp_path / "emission.lock"
    path.touch(mode=0o600)
    return path


def _attempt(lock: EmissionLock, pipe: Connection) -> None:
    try:
        with lock.held():
            pipe.send("acquired")
    except EmissionLockError as error:
        pipe.send(str(error))
    finally:
        pipe.close()


def _hold(lock: EmissionLock, pipe: Connection, execute: bool) -> None:
    with lock.held():
        pipe.send("held")
        pipe.recv()
        if execute:
            os.set_inheritable(pipe.fileno(), True)
            code = (
                "import sys; from multiprocessing.connection import Connection; "
                "p = Connection(int(sys.argv[1])); p.send('exec'); p.recv()"
            )
            os.execv(sys.executable, [sys.executable, "-I", "-c", code, str(pipe.fileno())])


@pytest.fixture
def processes() -> Iterator[list[Any]]:
    children: list[Any] = []
    yield children
    for child in children:
        if child.is_alive():
            child.terminate()
        child.join(10)
        assert not child.is_alive()


def _start(children: list[Any], target: Any, *args: Any) -> Connection:
    parent, child = multiprocessing.get_context("fork").Pipe()
    process = multiprocessing.get_context("fork").Process(target=target, args=(*args, child))
    process.start()
    children.append(process)
    child.close()
    return parent


def _holding(lock: EmissionLock, pipe: Connection) -> None:
    _hold(lock, pipe, False)


def _executing(lock: EmissionLock, pipe: Connection) -> None:
    _hold(lock, pipe, True)


def test_same_inode_objects_reject_nested_entry_and_release_after_exception(lock_path: Path) -> None:
    first, second = EmissionLock(lock_path), EmissionLock(lock_path)
    with pytest.raises(ValueError, match="body"), first.held():
        with pytest.raises(EmissionLockError, match="nested_lock"), second.held():
            pass
        raise ValueError("body")
    with second.held():
        pass


def test_another_thread_cannot_inherit_process_ownership(lock_path: Path) -> None:
    lock = EmissionLock(lock_path)
    result: list[str] = []

    def attempt() -> None:
        try:
            with EmissionLock(lock_path).held():
                result.append("acquired")
        except EmissionLockBusy as error:
            result.append(str(error))

    with lock.held():
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert result == ["lock_busy"]


def test_fork_child_cleanup_and_acquisition_do_not_unlock_parent(
    lock_path: Path, processes: list[Any]
) -> None:
    lock = EmissionLock(lock_path)
    with lock.held():
        with _start(processes, _attempt, lock) as pipe:
            assert pipe.poll(5) and pipe.recv() == "lock_busy"
        with open(lock_path, "rb") as independent, pytest.raises(BlockingIOError):
            fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with _start(processes, _attempt, lock) as pipe:
        assert pipe.poll(5) and pipe.recv() == "acquired"


def _exit_inherited_context(context: Any, pipe: Connection) -> None:
    context.__exit__(None, None, None)
    pipe.send("closed")


def test_child_exiting_an_inherited_context_does_not_unlock_parent(
    lock_path: Path, processes: list[Any]
) -> None:
    lock = EmissionLock(lock_path)
    context = lock.held()
    context.__enter__()
    try:
        with _start(processes, _exit_inherited_context, context) as pipe:
            assert pipe.poll(5) and pipe.recv() == "closed"
        with open(lock_path, "rb") as independent, pytest.raises(BlockingIOError):
            fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        context.__exit__(None, None, None)


def test_process_death_releases_the_kernel_lock(lock_path: Path, processes: list[Any]) -> None:
    lock = EmissionLock(lock_path)
    with _start(processes, _holding, lock) as pipe:
        assert pipe.poll(5) and pipe.recv() == "held"
        with pytest.raises(EmissionLockBusy), lock.held():
            pass
        processes[-1].terminate()
        processes[-1].join(5)
        with lock.held():
            pass


def test_exec_closes_the_lock_descriptor(lock_path: Path, processes: list[Any]) -> None:
    lock = EmissionLock(lock_path)
    with _start(processes, _executing, lock) as pipe:
        assert pipe.poll(5) and pipe.recv() == "held"
        pipe.send("execute")
        assert pipe.poll(5) and pipe.recv() == "exec"
        with lock.held():
            pass
        pipe.send("exit")


def test_runtime_never_creates_a_missing_lock(tmp_path: Path) -> None:
    path = tmp_path / "missing.lock"
    with pytest.raises(EmissionLockError, match="unsafe_or_missing_lock"):
        EmissionLock(path)
    assert not path.exists()


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666])
def test_nonprivate_file_refuses(lock_path: Path, mode: int) -> None:
    lock_path.chmod(mode)
    with pytest.raises(EmissionLockError):
        EmissionLock(lock_path)


def test_symlinks_hardlinks_and_directories_refuse(lock_path: Path) -> None:
    alias = lock_path.with_name("alias")
    alias.symlink_to(lock_path)
    with pytest.raises(EmissionLockError):
        EmissionLock(alias)
    alias.unlink()
    alias.hardlink_to(lock_path)
    with pytest.raises(EmissionLockError):
        EmissionLock(lock_path)
    alias.unlink()
    with pytest.raises(EmissionLockError):
        EmissionLock(lock_path.parent)


def test_retained_inode_replacement_refuses_during_and_after_hold(lock_path: Path) -> None:
    lock = EmissionLock(lock_path)
    with pytest.raises(EmissionLockError, match="lock_inode_changed"), lock.held():
        lock_path.unlink()
        lock_path.touch(mode=0o600)
        with pytest.raises(EmissionLockError, match="lock_inode_changed"):
            EmissionLock(lock_path)
    with pytest.raises(EmissionLockError, match="lock_inode_changed"), lock.held():
        pass


def test_unlinked_held_file_is_not_recreated(lock_path: Path) -> None:
    lock = EmissionLock(lock_path)
    with pytest.raises(EmissionLockError, match="unsafe_or_missing_lock"), lock.held():
        lock_path.unlink()
    assert not lock_path.exists()


def test_writable_ancestor_refuses(lock_path: Path) -> None:
    directory = lock_path.parent / "unsafe"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    path = directory / "lock"
    path.touch(mode=0o600)
    with pytest.raises(EmissionLockError):
        EmissionLock(path)


def test_relative_path_refuses() -> None:
    with pytest.raises(EmissionLockError, match="invalid_lock_path"):
        EmissionLock(Path("emission.lock"))
