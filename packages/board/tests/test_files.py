import asyncio
import base64
import dataclasses
import hashlib
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx2
import pytest
from board.app import create_app
from board.db import Database, migrate
from board.files import Declaration, Files, sweeper
from board.files.storage import Store
from board.settings import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal

    from .conftest import Signer, SignerFactory

DAY_SECONDS = 86400


def _app(client: TestClient) -> FastAPI:
    assert isinstance(client.app, FastAPI)
    return client.app


def _portal(client: TestClient) -> "BlockingPortal":
    assert client.portal is not None, "TestClient used outside its context manager"
    return client.portal


def _register(client: TestClient, *signers: "Signer") -> None:
    for each in signers:
        _app(client).state.registry.add_line(each.allowed_line)


def _signed_body(envelope: bytes, signature: str) -> dict[str, str]:
    return {"envelope": base64.urlsafe_b64encode(envelope).decode(), "signature": signature}


def _bearer(client: TestClient, signer: "Signer") -> dict[str, str]:
    r = client.post("/api/auth/challenge", json={"principal": signer.principal})
    assert r.status_code == 200, f"challenge refused: {r.status_code} {r.text}"
    challenge = r.json()["challenge"]
    r = client.post(
        "/api/auth/token", json=_signed_body(*signer.envelope("auth", "", {"challenge": challenge}))
    )
    assert r.status_code == 200, f"token refused: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _declaration(content: bytes, **declared: Any) -> dict[str, Any]:
    return {
        "name": "notes.txt",
        "content_type": "text/plain",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    } | declared


def _upload(
    client: TestClient, uploader: "Signer", content: bytes, action: str = "file.put", **declared: Any
) -> httpx2.Response:
    response: httpx2.Response = client.post(
        "/api/files",
        data=_signed_body(*uploader.envelope(action, "", _declaration(content, **declared))),
        files={"blob": ("blob", content, "application/octet-stream")},
    )
    return response


def _multipart_body(uploader: "Signer", content: bytes, order: tuple[str, ...]) -> tuple[bytes, str]:
    boundary = "board-test-boundary"
    fields = _signed_body(*uploader.envelope("file.put", "", _declaration(content)))
    values = {name: value.encode() for name, value in fields.items()} | {"blob": content}
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        + values[name]
        + b"\r\n"
        for name in order
    )
    return body + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


async def _in_chunks(data: bytes, size: int) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _uploaded(client: TestClient, uploader: "Signer", content: bytes, **declared: Any) -> str:
    r = _upload(client, uploader, content, **declared)
    assert r.status_code == 201, f"upload refused: {r.status_code} {r.text}"
    file_id: str = r.json()["id"]
    return file_id


def _listing(client: TestClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    r = client.get("/api/files", headers=headers)
    assert r.status_code == 200, f"listing failed: {r.status_code} {r.text}"
    listing: list[dict[str, Any]] = r.json()
    return listing


def _blobs_on_disk(settings: Settings) -> list[Path]:
    return sorted((settings.state_dir / "files").iterdir())


def _expire(settings: Settings, file_id: str) -> None:
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE files SET expires_at = ? WHERE id = ?", (time.time() - DAY_SECONDS, file_id))


def test_expired_file_is_removed_by_sweep(
    client: TestClient, signer: "SignerFactory", settings: Settings
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    headers = _bearer(client, alice)
    expired = _uploaded(client, alice, b"yesterday's hand-off")
    live_content = b"today's hand-off"
    live = _uploaded(client, alice, live_content)
    _expire(settings, expired)

    app = _app(client)
    _portal(client).call(sweeper.sweep, app.state.db, app.state.file_store, time.time())

    r = client.get(f"/api/files/{expired}", headers=headers)
    assert r.status_code == 404, f"expired file still served: {r.status_code}"
    path = settings.state_dir / "files" / expired
    assert not path.exists(), f"expired blob left on disk: {path}"
    with closing(sqlite3.connect(settings.db_path)) as conn:
        row = conn.execute("SELECT 1 FROM files WHERE id = ?", (expired,)).fetchone()
    assert row is None, f"expired row left in table: {expired}"
    r = client.get(f"/api/files/{live}", headers=headers)
    digest = hashlib.sha256(r.content).hexdigest()
    assert r.status_code == 200 and digest == hashlib.sha256(live_content).hexdigest(), (
        f"live file lost or corrupted: {r.status_code} {digest}"
    )


def test_unsigned_upload_rejected(client: TestClient, signer: "SignerFactory", settings: Settings) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    headers = _bearer(client, alice)

    r = client.post("/api/files", headers=headers, files={"blob": ("blob", b"no envelope", "text/plain")})

    assert r.status_code == 400, f"upload without envelope not refused: {r.status_code} {r.text}"
    assert r.json() == {"reason": "bad_upload", "field": "envelope"}, f"wrong refusal: {r.json()}"
    assert _listing(client, headers) == [], "unsigned upload was listed"
    assert _blobs_on_disk(settings) == [], f"unsigned upload left a blob: {_blobs_on_disk(settings)}"


@pytest.mark.anyio
async def test_upload_streamed_in_small_chunks_is_stored_intact(
    settings: Settings, signer: "SignerFactory"
) -> None:
    alice = signer("claude@testbox")
    content = bytes(range(256)) * 64
    body, content_type = _multipart_body(alice, content, ("envelope", "signature", "blob"))
    app = create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://board") as client,
    ):
        app.state.registry.add_line(alice.allowed_line)

        r = await client.post(
            "/api/files", content=_in_chunks(body, 7), headers={"content-type": content_type}
        )

    assert r.status_code == 201, f"upload split into 7-byte chunks refused: {r.status_code} {r.text}"
    assert r.json()["sha256"] == hashlib.sha256(content).hexdigest(), f"stored digest differs: {r.json()}"


def test_blob_before_signed_fields_rejected(
    client: TestClient, signer: "SignerFactory", settings: Settings
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    body, content_type = _multipart_body(alice, b"sent too early", ("blob", "envelope", "signature"))

    r = client.post("/api/files", content=body, headers={"content-type": content_type})

    assert r.status_code == 400, f"blob ahead of its envelope not refused: {r.status_code} {r.text}"
    assert r.json() == {"reason": "bad_upload", "field": "envelope"}, f"wrong refusal: {r.json()}"
    assert _blobs_on_disk(settings) == [], f"unverified blob left on disk: {_blobs_on_disk(settings)}"


def test_upload_with_other_action_rejected(
    client: TestClient, signer: "SignerFactory", settings: Settings
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)

    r = _upload(client, alice, b"signed for something else", action="post")

    assert r.status_code == 400, f"non-file.put envelope not refused: {r.status_code} {r.text}"
    assert r.json() == {"reason": "bad_upload", "field": "action"}, f"wrong refusal: {r.json()}"
    assert _blobs_on_disk(settings) == [], f"refused upload left a blob: {_blobs_on_disk(settings)}"


@pytest.mark.parametrize(
    "declared",
    [
        {"sha256": hashlib.sha256(b"other bytes").hexdigest()},
        {"size": len(b"the real bytes") + 1},
        {"size": len(b"the real bytes") - 1},
    ],
    ids=["wrong-sha256", "size-too-large", "size-too-small"],
)
def test_upload_with_wrong_digest_rejected(
    client: TestClient, signer: "SignerFactory", settings: Settings, declared: dict[str, Any]
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)

    r = _upload(client, alice, b"the real bytes", **declared)

    assert r.status_code == 400, f"mismatched upload not refused: {r.status_code} {r.text}"
    assert r.json() == {"reason": "digest_mismatch"}, f"wrong refusal: {r.json()}"
    assert _listing(client, _bearer(client, alice)) == [], "mismatched upload was listed"
    assert _blobs_on_disk(settings) == [], f"mismatched upload left a blob: {_blobs_on_disk(settings)}"


def test_upload_over_limit_rejected(signer: "SignerFactory", settings: Settings) -> None:
    alice = signer("claude@testbox")
    with TestClient(create_app(dataclasses.replace(settings, file_max_mb=1))) as client:
        _register(client, alice)

        r = _upload(client, alice, b"x" * (1024 * 1024 + 1))

        assert r.status_code == 413, f"oversized upload not refused: {r.status_code} {r.text}"
        assert r.json() == {"reason": "file_too_large"}, f"wrong refusal: {r.json()}"
        assert _blobs_on_disk(settings) == [], f"oversized upload left a blob: {_blobs_on_disk(settings)}"


def test_ttl_days_capped_at_30(client: TestClient, signer: "SignerFactory") -> None:
    alice = signer("claude@testbox")
    _register(client, alice)

    before = time.time()
    r = _upload(client, alice, b"keep me for a quarter", ttl_days=90)
    after = time.time()

    assert r.status_code == 201, f"upload refused: {r.status_code} {r.text}"
    expires_at = datetime.fromisoformat(r.json()["expires_at"]).timestamp()
    assert before + 30 * DAY_SECONDS - 1 <= expires_at <= after + 30 * DAY_SECONDS, (
        f"ttl_days=90 not capped at 30 days: {r.json()['expires_at']}"
    )


def test_files_list_shows_uploader_and_expiry(
    client: TestClient, signer: "SignerFactory", settings: Settings
) -> None:
    alice, bob = signer("claude@testbox"), signer("pi@testbox")
    _register(client, alice, bob)
    from_alice = _upload(client, alice, b"alice's notes").json()
    from_bob = _upload(client, bob, b"bob's notes", ttl_days=2).json()
    _expire(settings, _uploaded(client, alice, b"alice's stale notes"))

    listing = _listing(client, _bearer(client, alice))

    seen = {entry["id"]: (entry["uploaded_by"], entry["expires_at"]) for entry in listing}
    assert seen == {
        from_alice["id"]: ("claude@testbox", from_alice["expires_at"]),
        from_bob["id"]: ("pi@testbox", from_bob["expires_at"]),
    }, f"listing does not show the live files with uploader and expiry: {listing}"


@pytest.mark.parametrize(
    ("name", "disposition"),
    [
        ('../../etc/pass"wd\r\n.txt', "attachment; filename=\"pass_wd.txt\"; filename*=UTF-8''pass_wd.txt"),
        (
            "rapport-été.pdf",
            "attachment; filename=\"rapport-_t_.pdf\"; filename*=UTF-8''rapport-%C3%A9t%C3%A9.pdf",
        ),
        ("..", "attachment; filename=\"download\"; filename*=UTF-8''download"),
    ],
    ids=["traversal-and-header-injection", "non-ascii", "dot-dot"],
)
def test_download_uses_sanitised_name(
    client: TestClient, signer: "SignerFactory", name: str, disposition: str
) -> None:
    alice = signer("claude@testbox")
    _register(client, alice)
    file_id = _uploaded(client, alice, b"named bytes", name=name)

    r = client.get(f"/api/files/{file_id}", headers=_bearer(client, alice))

    assert r.status_code == 200, f"download failed: {r.status_code} {r.text}"
    assert r.headers["content-disposition"] == disposition, (
        f"unsafe Content-Disposition: {r.headers['content-disposition']!r}"
    )


class _CancelledInsert:
    async def transact(self, fn: object) -> None:
        raise asyncio.CancelledError


@pytest.mark.anyio
async def test_cancelled_insert_keeps_the_blob(tmp_path: Path) -> None:
    store = Store(tmp_path / "blobs", max_bytes=1024)
    files = Files(cast("Database", _CancelledInsert()), store, default_ttl_days=1)
    content = b"payload"
    declared = Declaration("a.txt", "text/plain", len(content), hashlib.sha256(content).hexdigest())

    with pytest.raises(asyncio.CancelledError):
        await files.put(_in_chunks(content, 4), declared, "claude@testbox")

    assert list((tmp_path / "blobs").iterdir()), "cancellation deleted a blob whose row may still commit"


class _ThreadRecordingStore(Store):
    def __init__(self, root: Path) -> None:
        super().__init__(root, max_bytes=1024)
        self.delete_threads: list[int] = []

    def delete(self, blob_id: str) -> None:
        self.delete_threads.append(threading.get_ident())
        super().delete(blob_id)


@pytest.mark.anyio
async def test_sweep_unlinks_blobs_off_the_event_loop(tmp_path: Path) -> None:
    db = Database(tmp_path / "board.sqlite3")
    await db.transact(migrate)
    store = _ThreadRecordingStore(tmp_path / "blobs")
    await db.transact(
        lambda conn: conn.execute(
            "INSERT INTO files (id, name, content_type, size, sha256, uploaded_by, created_at, expires_at) "
            "VALUES (?, 'a', 'text/plain', 0, ?, 'claude@testbox', 0, 1)",
            (str(uuid.uuid4()), "0" * 64),
        )
    )

    await sweeper.sweep(db, store, now=time.time())

    loop_thread = threading.get_ident()
    assert store.delete_threads, "sweep deleted nothing"
    assert loop_thread not in store.delete_threads, "sweep unlinked blobs on the event loop thread"
