import base64
import binascii
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Self

from board.constants import DEFAULT_NAMESPACE

_KEY_TYPE = "ssh-ed25519"
_ED25519_BLOB_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
_ED25519_BLOB_BYTES = len(_ED25519_BLOB_PREFIX) + 32
_PRINCIPAL = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+")


class BadSignerLine(ValueError):
    def __init__(self, reason: str, line: str) -> None:
        super().__init__(f"{reason}: {line!r}")
        self.reason = reason


def _namespaces_option(namespace: str) -> str:
    return f'namespaces="{namespace}"'


def allowed_signers_line(principal: str, public_key: str, namespace: str = DEFAULT_NAMESPACE) -> str:
    return f"{principal} {_namespaces_option(namespace)} {public_key}"


@dataclass(frozen=True)
class _Signer:
    principal: str
    public_key: str

    @classmethod
    def parse(cls, line: str, namespace: str) -> Self:
        fields = line.split()
        if len(fields) != 4:
            raise BadSignerLine("expected <kind>@<box> namespaces=... ssh-ed25519 <key>", line)
        principal, options, key_type, blob = fields
        if not _PRINCIPAL.fullmatch(principal):
            raise BadSignerLine("principal must be <kind>@<box>", line)
        if options != _namespaces_option(namespace):
            raise BadSignerLine(f"options must be exactly {_namespaces_option(namespace)}", line)
        return cls(principal, _public_key(key_type, blob, line))

    @classmethod
    def from_registry(cls, line: str) -> Self | None:
        fields = line.split()
        if len(fields) != 4 or line.startswith("#"):
            return None
        return cls(fields[0], f"{fields[2]} {fields[3]}")

    def line(self, namespace: str, valid_before: date | None = None) -> str:
        if valid_before is None:
            return allowed_signers_line(self.principal, self.public_key, namespace)
        option = _namespaces_option(namespace)
        return f'{self.principal} valid-before="{valid_before:%Y%m%d}Z",{option} {self.public_key}'


class Registry:
    def __init__(self, state_dir: Path, namespace: str = DEFAULT_NAMESPACE) -> None:
        self.namespace = namespace
        self._allowed_signers = state_dir / "allowed_signers"
        self._revoked_keys = state_dir / "revoked_keys"
        # ssh-keygen -Y verify -r fails every verification when the deny-list file is missing (probe 26).
        self._allowed_signers.touch(exist_ok=True)
        self._revoked_keys.touch(exist_ok=True)

    def verification_options(self) -> list[str]:
        return ["-f", str(self._allowed_signers), "-r", str(self._revoked_keys)]

    def add_line(self, line: str) -> bool:
        return self._add([_Signer.parse(line, self.namespace)]) == 1

    def import_lines(self, text: str) -> int:
        return self._add([_Signer.parse(line, self.namespace) for line in text.splitlines() if line.strip()])

    def revoke_after(self, principal: str, before: date) -> int:
        lines = _read_lines(self._allowed_signers)
        revoked = 0
        for index, line in enumerate(lines):
            signer = _Signer.from_registry(line)
            if signer is not None and signer.principal == principal:
                lines[index] = signer.line(self.namespace, valid_before=before)
                revoked += 1
        if revoked:
            _write_lines(self._allowed_signers, lines)
        return revoked

    def deny_key(self, text: str) -> bool:
        fields = text.split()
        if _KEY_TYPE not in fields[:-1]:
            raise BadSignerLine(f"no {_KEY_TYPE} public key found", text)
        public_key = _public_key(_KEY_TYPE, fields[fields.index(_KEY_TYPE) + 1], text)
        denied = _read_lines(self._revoked_keys)
        if public_key in denied:
            return False
        _write_lines(self._revoked_keys, [*denied, public_key])
        return True

    def principals(self) -> set[str]:
        return {signer.principal for signer in self._signers()}

    def known(self, principal: str) -> bool:
        return principal in self.principals()

    def _signers(self) -> list[_Signer]:
        parsed = (_Signer.from_registry(line) for line in _read_lines(self._allowed_signers))
        return [signer for signer in parsed if signer is not None]

    def _add(self, signers: list[_Signer]) -> int:
        present = set(self._signers())
        added = [signer for signer in dict.fromkeys(signers) if signer not in present]
        if added:
            lines = _read_lines(self._allowed_signers)
            _write_lines(self._allowed_signers, [*lines, *(signer.line(self.namespace) for signer in added)])
        return len(added)


def _public_key(key_type: str, blob: str, line: str) -> str:
    if key_type != _KEY_TYPE:
        raise BadSignerLine(f"key type must be {_KEY_TYPE}", line)
    try:
        raw = base64.b64decode(blob, validate=True)
    except binascii.Error:
        raw = b""
    if len(raw) != _ED25519_BLOB_BYTES or not raw.startswith(_ED25519_BLOB_PREFIX):
        raise BadSignerLine(f"not a valid {_KEY_TYPE} public key", line)
    return f"{key_type} {blob}"


def _read_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    # The running server's ssh-keygen may read this file at any moment; replacing it atomically means
    # it never sees a truncated registry and rejects a good signature.
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as staged:
        staged.write("".join(f"{line}\n" for line in lines))
    os.replace(staged.name, path)
