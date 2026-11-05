import json
import secrets
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from board.db import Database, migrate
from board.identity.registry import Registry, allowed_signers_line
from board.identity.verifier import SSH_KEYGEN_TIMEOUT_SECONDS, Rejected, Verifier, rfc3339
from board.settings import Settings

PRINCIPAL = "selftest@board"
_PROBE = b"selftest-probe"
_TAMPERED_PROBE = b"selftest-probE"
_LONG_AGO = date(2000, 1, 1)


class SelfTestFailed(RuntimeError):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"startup self-test failed at step {step!r}: {detail}")
        self.step = step
        self.detail = detail


async def run(settings: Settings, tmp_dir: Path) -> None:
    key_path = tmp_dir / "selftest_ed25519"
    with _step("keygen"):
        public_key, fingerprint = await run_in_threadpool(_generate_key, settings.ssh_keygen, key_path)
    with _step("sign"):
        message = _probe_envelope()
        signature = await run_in_threadpool(
            _sign, settings.ssh_keygen, key_path, message, settings.signing_namespace
        )
    with _step("migrate"):
        db = Database(tmp_dir / "selftest.sqlite3")
        await db.transact(migrate)

    def registry_with_key(name: str) -> tuple[Registry, Verifier]:
        registry_dir = tmp_dir / name
        registry_dir.mkdir()
        registry = Registry(registry_dir, settings.signing_namespace)
        registry.add_line(allowed_signers_line(PRINCIPAL, public_key, settings.signing_namespace))
        return registry, Verifier(registry, db, settings.ts_window_seconds, settings.ssh_keygen)

    with _step("verify"):
        _, verifier = registry_with_key("accepting")
        verified = await verifier.verify(message, signature)
        if verified.key_fingerprint != fingerprint:
            raise RuntimeError(f"matched key {verified.key_fingerprint}, expected {fingerprint}")
    with _step("tampered"):
        await _expect_bad_signature(verifier, message.replace(_PROBE, _TAMPERED_PROBE), signature)
    with _step("revoked"):
        registry, verifier = registry_with_key("revoked")
        registry.revoke_after(PRINCIPAL, _LONG_AGO)
        await _expect_bad_signature(verifier, message, signature)
    with _step("denied"):
        registry, verifier = registry_with_key("denied")
        registry.deny_key(public_key)
        await _expect_bad_signature(verifier, message, signature)


@contextmanager
def _step(name: str) -> Iterator[None]:
    try:
        yield
    except Exception as failure:
        raise SelfTestFailed(name, f"{type(failure).__name__}: {failure}") from failure


async def _expect_bad_signature(verifier: Verifier, message: bytes, signature: str) -> None:
    try:
        await verifier.verify(message, signature)
    except Rejected as rejected:
        if rejected.reason != "bad_signature":
            raise RuntimeError(f"rejected as {rejected.reason}, expected bad_signature") from rejected
        return
    raise RuntimeError("accepted, expected bad_signature")


def _probe_envelope() -> bytes:
    envelope = {
        "v": 1,
        "principal": PRINCIPAL,
        "ts": rfc3339(time.time()),
        "nonce": secrets.token_urlsafe(24),
        "action": "selftest",
        "target": "",
        "payload": {"probe": _PROBE.decode()},
    }
    return json.dumps(envelope, separators=(",", ":")).encode()


def _generate_key(ssh_keygen: str, key_path: Path) -> tuple[str, str]:
    _ssh_keygen(ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", PRINCIPAL, "-f", str(key_path))
    public_key_path = Path(f"{key_path}.pub")
    key_type, blob = public_key_path.read_text().split()[:2]
    fingerprint = _ssh_keygen(ssh_keygen, "-l", "-E", "sha256", "-f", str(public_key_path)).split()[1]
    return f"{key_type} {blob}", fingerprint.decode()


def _sign(ssh_keygen: str, key_path: Path, message: bytes, namespace: str) -> str:
    return _ssh_keygen(ssh_keygen, "-Y", "sign", "-f", str(key_path), "-n", namespace, stdin=message).decode()


def _ssh_keygen(ssh_keygen: str, *args: str, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(
        [ssh_keygen, *args], input=stdin, capture_output=True, timeout=SSH_KEYGEN_TIMEOUT_SECONDS, check=False
    )
    if completed.returncode != 0:
        command = " ".join([ssh_keygen, *args])
        raise RuntimeError(f"{command} exited {completed.returncode}: {completed.stderr.decode().strip()}")
    return completed.stdout
