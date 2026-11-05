import hashlib
import secrets

_SCHEME = "scrypt"
_N = 2**14
_R = 8
_P = 1
_SALT_BYTES = 16


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(pw, salt, _N, _R, _P)
    return f"{_SCHEME}:{_N}:{_R}:{_P}:{salt.hex()}:{digest.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    n, r, p, salt, expected = _parse(stored)
    return secrets.compare_digest(_scrypt(pw, salt, n, r, p), expected)


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes]:
    fields = stored.split(":")
    try:
        if len(fields) != 6 or fields[0] != _SCHEME:
            raise ValueError
        return (
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
            bytes.fromhex(fields[4]),
            bytes.fromhex(fields[5]),
        )
    except ValueError:
        raise ValueError("stored password hash was not produced by board-serve hash-password") from None


def _scrypt(pw: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p)
