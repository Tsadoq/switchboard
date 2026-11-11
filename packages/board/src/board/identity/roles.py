import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from board.identity.verifier import Rejected, Verified


@dataclass(frozen=True)
class VerifierRole:
    box: str
    fingerprint: str


class VerifierRoles:
    def __init__(self, roles: dict[str, VerifierRole] | None = None) -> None:
        self._roles = dict(roles or {})

    @classmethod
    def load(cls, path: Path | None) -> Self:
        if path is None:
            return cls()
        values = json.loads(path.read_text(), object_pairs_hook=_unique, parse_constant=_constant)
        if not isinstance(values, dict):
            raise TypeError("verifier roles must be an object")
        roles: dict[str, VerifierRole] = {}
        for principal, value in values.items():
            if not isinstance(value, dict) or set(value) != {"box", "fingerprint"}:
                raise ValueError("invalid verifier role fields")
            box, fingerprint = value["box"], value["fingerprint"]
            if (
                not isinstance(box, str)
                or re.fullmatch(r"[a-z0-9_-]{1,64}", box) is None
                or box in ("ops", "heimdall")
                or principal != f"verifier@{box}"
                or not isinstance(fingerprint, str)
                or re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint) is None
            ):
                raise ValueError("invalid verifier identity binding")
            roles[principal] = VerifierRole(box, fingerprint)
        return cls(roles)

    @property
    def principals(self) -> frozenset[str]:
        return frozenset(self._roles)

    def require_agent(self, principal: str) -> None:
        if principal in self._roles:
            raise Rejected("forbidden_role")

    def is_service(self, principal: str) -> bool:
        return principal in self._roles

    def authorize(self, verified: Verified) -> str:
        role = self._roles.get(verified.envelope.principal)
        if role is None or verified.key_fingerprint != role.fingerprint:
            raise Rejected("forbidden_role")
        return role.box


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate verifier role key")
        values[key] = value
    return values


def _constant(value: str) -> None:
    raise ValueError("nonfinite verifier role value")
