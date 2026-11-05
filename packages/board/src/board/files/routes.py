import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from board.files import Declaration, FileEntry, Files, NoSuchFile
from board.files.storage import DigestMismatch, TooLarge
from board.identity.routes import decode_envelope
from board.identity.verifier import Verifier, rfc3339

type Reader = Callable[..., Awaitable[str]]

FILE_PUT = "file.put"
BLOB = "blob"
_SIGNED_FIELDS = ("envelope", "signature")
_FIELD_MAX_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_FALLBACK_NAME = "download"


class BadUpload(ValueError):
    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def install(app: FastAPI, files: Files, reader: Reader) -> None:
    app.include_router(_upload_route(files))
    app.include_router(_read_routes(files, reader))
    app.exception_handler(BadUpload)(_bad_upload)
    app.exception_handler(TooLarge)(_too_large)
    app.exception_handler(DigestMismatch)(_digest_mismatch)
    app.exception_handler(NoSuchFile)(_no_such_file)


def _upload_route(files: Files) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/files")
    async def upload(request: Request) -> JSONResponse:
        parts = _multipart(request)
        fields = await _signed_fields(parts)
        verifier: Verifier = request.app.state.verifier
        verified = await verifier.verify(decode_envelope(fields["envelope"]), fields["signature"])
        if verified.envelope.action != FILE_PUT:
            raise BadUpload("action")
        declared = Declaration.from_payload(verified.envelope.payload)
        entry = await files.put(_blob_chunks(parts), declared, verified.envelope.principal, signed=verified)
        return JSONResponse(
            {
                "id": entry.id,
                "sha256": entry.sha256,
                "size": entry.size,
                "expires_at": rfc3339(entry.expires_at),
            },
            status_code=201,
        )

    return router


def _read_routes(files: Files, reader: Reader) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(reader)])

    @router.get("/files")
    async def list_files() -> list[dict[str, Any]]:
        return [_entry_json(entry) for entry in await files.live()]

    @router.get("/files/{file_id}")
    async def download(file_id: str) -> StreamingResponse:
        entry, blob = await files.open(file_id)
        return StreamingResponse(
            _read_chunks(blob),
            media_type=entry.content_type,
            headers={
                "Content-Length": str(entry.size),
                "Content-Disposition": _attachment(entry.name),
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


@dataclass(frozen=True)
class _PartStart:
    name: str


@dataclass(frozen=True)
class _PartData:
    chunk: bytes


@dataclass(frozen=True)
class _PartEnd:
    pass


type _Event = _PartStart | _PartData | _PartEnd


class _MultipartEvents:
    def __init__(self, boundary: bytes) -> None:
        self._pending: list[_Event] = []
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._disposition = b""
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._begin_part,
                "on_header_field": lambda data, start, end: self._header_name.extend(data[start:end]),
                "on_header_value": lambda data, start, end: self._header_value.extend(data[start:end]),
                "on_header_end": self._end_header,
                "on_headers_finished": self._start_part,
                "on_part_data": lambda data, start, end: self._pending.append(_PartData(data[start:end])),
                "on_part_end": lambda: self._pending.append(_PartEnd()),
            },
        )

    async def read(self, body: AsyncIterable[bytes]) -> AsyncIterator[_Event]:
        async for chunk in body:
            try:
                self._parser.write(chunk)
            except FormParserError:
                raise BadUpload("body") from None
            events, self._pending = self._pending, []
            for event in events:
                yield event

    def _begin_part(self) -> None:
        self._disposition = b""

    def _end_header(self) -> None:
        if self._header_name.lower() == b"content-disposition":
            self._disposition = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def _start_part(self) -> None:
        _, options = parse_options_header(self._disposition)
        self._pending.append(_PartStart(options.get(b"name", b"").decode(errors="replace")))


def _multipart(request: Request) -> AsyncIterator[_Event]:
    media_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary:
        raise BadUpload("content-type")
    return _MultipartEvents(boundary).read(request.stream())


async def _signed_fields(parts: AsyncIterator[_Event]) -> dict[str, str]:
    values: dict[str, bytearray] = {}
    current = ""
    async for event in parts:
        if isinstance(event, _PartStart):
            if event.name == BLOB:
                break
            if event.name not in _SIGNED_FIELDS or event.name in values:
                raise BadUpload(event.name)
            current = event.name
            values[current] = bytearray()
        elif isinstance(event, _PartData):
            values[current] += event.chunk
            if len(values[current]) > _FIELD_MAX_BYTES:
                raise BadUpload(current)
    else:
        raise BadUpload(BLOB)
    for name in _SIGNED_FIELDS:
        if name not in values:
            raise BadUpload(name)
    return {name: value.decode(errors="replace") for name, value in values.items()}


async def _blob_chunks(parts: AsyncIterator[_Event]) -> AsyncIterator[bytes]:
    async for event in parts:
        if isinstance(event, _PartData):
            yield event.chunk
        elif isinstance(event, _PartEnd):
            return
    raise BadUpload(BLOB)


def _read_chunks(blob: BinaryIO) -> Iterator[bytes]:
    with blob:
        while chunk := blob.read(_READ_CHUNK_BYTES):
            yield chunk


def _attachment(name: str) -> str:
    last_component = re.split(r"[/\\]", name)[-1]
    printable = "".join(ch for ch in last_component if ch.isprintable()).replace('"', "_").strip()
    safe = _FALLBACK_NAME if printable in ("", ".", "..") else printable
    ascii_fallback = "".join(ch if ch.isascii() else "_" for ch in safe)
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(safe, safe='')}"


def _entry_json(entry: FileEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.name,
        "content_type": entry.content_type,
        "size": entry.size,
        "sha256": entry.sha256,
        "uploaded_by": entry.uploaded_by,
        "created_at": rfc3339(entry.created_at),
        "expires_at": rfc3339(entry.expires_at),
    }


async def _bad_upload(request: Request, bad: BadUpload) -> JSONResponse:
    return JSONResponse({"reason": "bad_upload", "field": bad.field}, status_code=400)


async def _too_large(request: Request, too_large: TooLarge) -> JSONResponse:
    return JSONResponse({"reason": "file_too_large"}, status_code=413)


async def _digest_mismatch(request: Request, mismatch: DigestMismatch) -> JSONResponse:
    return JSONResponse({"reason": "digest_mismatch"}, status_code=400)


async def _no_such_file(request: Request, missing: NoSuchFile) -> JSONResponse:
    return JSONResponse({"reason": "no_such_file"}, status_code=404)
