import errno
import fcntl
import json
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

CONSUMER_ENV = "AGENT_BOARD_CONSUMER"
CURSOR_DIR = "cursors"
RETIRED = "RETIRED"
LOCK_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05
_CONSUMER = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VERSION = 1


class CursorError(RuntimeError):
    pass


class CursorUnsafe(CursorError):
    """A cursor path that is not a private regular file of ours; never followed, repaired or replaced."""


class CursorCorrupt(CursorError):
    """A private regular cursor file whose content cannot be read; only an explicit recovery replaces it."""


def consumer_id(value: str | None) -> str:
    if value is None or _CONSUMER.fullmatch(value) is None:
        raise CursorError(f"{CONSUMER_ENV} is missing or not a consumer id")
    return value


class Cursors:
    """One consumer's read positions, one file per thread, changed only while the consumer's lock is held.

    A position means the posts up to it were written to stdout and flushed; not that anyone read or handled them.
    """

    def __init__(self, directory: Path, consumer: str) -> None:
        self._directory = directory / consumer
        self._consumer = consumer

    @property
    def retired(self) -> bool:
        return _private_file(self._directory / RETIRED, missing_ok=True)

    def get(self, thread_id: int) -> int | None:
        path = self._path(thread_id)
        if not _private_file(path, missing_ok=True):
            return None
        try:
            recorded = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, ValueError):
            raise CursorCorrupt(f"read cursor for thread {thread_id} is unreadable") from None
        post = recorded.get("post") if isinstance(recorded, dict) else None
        if (
            not isinstance(recorded, dict)
            or recorded.get("v") != _VERSION
            or type(post) is not int
            or post < 0
        ):
            raise CursorCorrupt(f"read cursor for thread {thread_id} is unreadable")
        return post

    def advance(self, thread_id: int, post: int) -> None:
        """Moves the position up to `post`; never down."""
        current = self.get(thread_id)
        if current is not None and post <= current:
            return
        self._write(self._path(thread_id), json.dumps({"v": _VERSION, "post": post}))

    def set_aside(self, thread_id: int) -> Path:
        """Moves an unreadable cursor aside as evidence; refuses anything that is not a corrupt private file."""
        path = self._path(thread_id)
        _private_file(path, missing_ok=False)
        try:
            self.get(thread_id)
        except CursorCorrupt:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            aside = path.with_name(f"{path.name}.corrupt-{stamp}")
            os.replace(path, aside)
            _sync_directory(self._directory)
            return aside
        raise CursorError(f"read cursor for thread {thread_id} is valid; nothing to recover")

    def retire(self) -> None:
        self._write(self._directory / RETIRED, self._consumer)

    def threads(self) -> dict[int, int]:
        if not self._directory.exists():
            return {}
        positions: dict[int, int] = {}
        for path in self._directory.iterdir():
            if path.name.endswith(".json") and path.stem.isdigit() and path.stem == str(int(path.stem)):
                post = self.get(int(path.stem))
                if post is not None:
                    positions[int(path.stem)] = post
        return positions

    def _path(self, thread_id: int) -> Path:
        if type(thread_id) is not int or thread_id <= 0:
            raise CursorError("not a thread id")
        return self._directory / f"{thread_id}.json"

    def _write(self, path: Path, text: str) -> None:
        _private_directory(self._directory)
        if os.path.lexists(path):
            _private_file(path, missing_ok=False)
        staged = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, text.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(staged, path)
        _sync_directory(self._directory)


@contextmanager
def locked(directory: Path, consumer: str, timeout: float = LOCK_SECONDS) -> Iterator[Cursors]:
    """Holds the consumer's stable lock file, which is never replaced, for the whole read, flush and commit."""
    consumer = consumer_id(consumer)
    _private_directory(directory)
    path = directory / f"{consumer}.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise CursorUnsafe("read cursor lock is not a private regular file") from None
    try:
        _check_lock(path, fd)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= deadline:
                    raise CursorError("read cursor is busy") from None
                time.sleep(_LOCK_POLL_SECONDS)
        _check_lock(path, fd)
        yield Cursors(directory, consumer)
    finally:
        os.close(fd)


def hand_off(directory: Path, retiring: str, successor: str) -> dict[int, int]:
    """Retires a consumer durably, then copies its positions to a successor that has none for those threads.

    Retirement is written under the retiring consumer's lock before anything is copied, so a CLI still running
    for it refuses from then on; a crash before the copy leaves the successor empty, which only replays.
    """
    with locked(directory, retiring) as old:
        old.retire()
        positions = old.threads()
        with locked(directory, successor) as new:
            copied = {}
            for thread_id, post in positions.items():
                if new.get(thread_id) is None:
                    new.advance(thread_id, post)
                    copied[thread_id] = post
            return copied


def _check_lock(path: Path, fd: int) -> None:
    held = os.fstat(fd)
    try:
        named = path.lstat()
    except OSError:
        raise CursorUnsafe("read cursor lock was removed") from None
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_uid != os.geteuid()
        or held.st_mode & 0o077
        or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)
    ):
        raise CursorUnsafe("read cursor lock is not a private regular file")


def _private_file(path: Path, *, missing_ok: bool) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise CursorError(f"no read cursor at {path.name}") from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise CursorUnsafe(f"{path.name} is not a private regular file")
    return True


def _private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise CursorUnsafe(f"{directory} is not a private directory")


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
