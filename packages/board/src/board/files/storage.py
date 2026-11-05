import contextlib
import hashlib
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from starlette.concurrency import run_in_threadpool


class TooLarge(Exception):
    pass


class DigestMismatch(Exception):
    pass


@dataclass(frozen=True)
class StoredBlob:
    id: str
    sha256: str
    size: int


class Store:
    def __init__(self, root: Path, max_bytes: int) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root = root
        self._max_bytes = max_bytes

    async def put(self, stream: AsyncIterable[bytes], expected_sha256: str, expected_size: int) -> StoredBlob:
        if expected_size > self._max_bytes:
            raise TooLarge
        blob_id = str(uuid.uuid4())
        path = self._path(blob_id)
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("xb") as out:
                async for chunk in stream:
                    size += len(chunk)
                    if size > expected_size:
                        raise DigestMismatch
                    digest.update(chunk)
                    await run_in_threadpool(out.write, chunk)
            if size != expected_size or digest.hexdigest() != expected_sha256:
                raise DigestMismatch
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return StoredBlob(blob_id, digest.hexdigest(), size)

    def open(self, blob_id: str) -> BinaryIO:
        return self._path(blob_id).open("rb")

    def delete(self, blob_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(blob_id).unlink()

    def _path(self, blob_id: str) -> Path:
        if not _is_canonical_uuid(blob_id):
            raise FileNotFoundError(blob_id)
        return self._root / blob_id


def _is_canonical_uuid(text: str) -> bool:
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False
