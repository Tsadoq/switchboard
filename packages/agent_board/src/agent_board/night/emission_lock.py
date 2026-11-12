import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from agent_board.night_state import StateError, _check_file, check_private_file

type Identity = tuple[int, int]


class EmissionLockError(StateError):
    pass


class EmissionLockBusy(EmissionLockError):
    pass


@dataclass
class _Coordinator:
    path: Path
    fd: int
    identity: Identity
    pid: int = field(default_factory=os.getpid)
    mutex: threading.Lock = field(default_factory=threading.Lock)
    owner: int | None = None


_coordinators: dict[Identity, _Coordinator] = {}
_paths: dict[Path, Identity] = {}
_registry_mutex = threading.Lock()


def _before_fork() -> None:
    _registry_mutex.acquire()


def _after_parent_fork() -> None:
    _registry_mutex.release()


def _after_fork() -> None:
    global _registry_mutex
    for coordinator in _coordinators.values():
        if coordinator.fd >= 0:
            try:
                os.close(coordinator.fd)
            except OSError:
                pass
            coordinator.fd = -1
    _coordinators.clear()
    _paths.clear()
    _registry_mutex = threading.Lock()


os.register_at_fork(before=_before_fork, after_in_parent=_after_parent_fork, after_in_child=_after_fork)


def _identity(metadata: os.stat_result) -> Identity:
    return metadata.st_dev, metadata.st_ino


def _metadata(path: Path) -> os.stat_result:
    check_private_file(path)
    metadata = path.lstat()
    _check_file(metadata)
    return metadata


def _verify(coordinator: _Coordinator) -> None:
    try:
        metadata = _metadata(coordinator.path)
        retained = os.fstat(coordinator.fd)
        if _identity(metadata) != coordinator.identity or _identity(retained) != coordinator.identity:
            raise EmissionLockError("lock_inode_changed")
        _check_file(retained)
    except (OSError, StateError) as error:
        if isinstance(error, EmissionLockError):
            raise
        raise EmissionLockError("unsafe_or_missing_lock") from None


def _coordinator(path: Path, expected: Identity | None = None) -> _Coordinator:
    if not _registry_mutex.acquire(blocking=False):
        raise EmissionLockBusy("lock_registry_busy")
    try:
        try:
            identity = _identity(_metadata(path))
        except (OSError, StateError):
            raise EmissionLockError("unsafe_or_missing_lock") from None
        if (expected is not None and identity != expected) or _paths.get(path, identity) != identity:
            raise EmissionLockError("lock_inode_changed")
        existing = _coordinators.get(identity)
        if existing is not None:
            _verify(existing)
            return existing
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise EmissionLockError("unsafe_or_missing_lock") from None
        candidate = _Coordinator(path, fd, identity)
        try:
            _verify(candidate)
        except BaseException:
            os.close(fd)
            raise
        _coordinators[identity] = candidate
        _paths[path] = identity
        return candidate
    finally:
        _registry_mutex.release()


class EmissionLock:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise EmissionLockError("invalid_lock_path")
        self._path = path
        self._identity = _coordinator(path).identity

    @contextmanager
    def held(self) -> Iterator[None]:
        coordinator = _coordinator(self._path, self._identity)
        if not coordinator.mutex.acquire(blocking=False):
            if coordinator.owner == threading.get_ident():
                raise EmissionLockError("nested_lock")
            raise EmissionLockBusy("lock_busy")
        acquired = False
        try:
            try:
                fcntl.flock(coordinator.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EmissionLockBusy("lock_busy") from None
            except OSError:
                raise EmissionLockError("lock_unavailable") from None
            acquired = True
            coordinator.owner = threading.get_ident()
            _verify(coordinator)
            yield
            if coordinator.pid == os.getpid():
                _verify(coordinator)
        finally:
            if coordinator.pid == os.getpid():
                coordinator.owner = None
                try:
                    if acquired:
                        try:
                            fcntl.flock(coordinator.fd, fcntl.LOCK_UN)
                        except OSError:
                            fd, coordinator.fd = coordinator.fd, -1
                            os.close(fd)
                            raise EmissionLockError("lock_release_failed") from None
                finally:
                    coordinator.mutex.release()
