import base64
import math
import os
import secrets
import ssl
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import certifi
import httpx
import rfc8785


@dataclass(frozen=True)
class ServerGrant:
    principal: str
    box: str
    state: str
    remaining_seconds: float
    window_end_utc: float


class GrantUnavailable(RuntimeError):
    pass


class AuthorityConfig(Protocol):
    @property
    def principal(self) -> str: ...
    @property
    def box(self) -> str: ...
    @property
    def namespace(self) -> str: ...
    @property
    def board_url(self) -> str: ...
    @property
    def ca_path(self) -> Path | None: ...
    @property
    def signer_key(self) -> Path: ...


class AuthorityHTTP(Protocol):
    def post(self, url: str, *, json: dict[str, Any], follow_redirects: bool) -> httpx.Response: ...


class NightAuthorityClient:
    def __init__(
        self,
        config: AuthorityConfig,
        *,
        http: AuthorityHTTP | None = None,
        signer: Callable[[bytes], str] | None = None,
    ) -> None:
        url = urlsplit(config.board_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise GrantUnavailable("unsafe_board_url")
        self._config = config
        self._http = http
        self._signer = signer or self._sign

    def request(self, action: str, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "v": 1,
            "principal": self._config.principal,
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "nonce": secrets.token_urlsafe(24),
            "action": action,
            "target": target,
            "payload": payload,
        }
        try:
            envelope = rfc8785.dumps(fields)
            body = {
                "envelope": base64.urlsafe_b64encode(envelope).decode(),
                "signature": self._signer(envelope),
            }
            if self._http is not None:
                response = self._http.post("/api/night/verifier", json=body, follow_redirects=False)
            else:
                context = ssl.create_default_context(
                    cafile=str(self._config.ca_path) if self._config.ca_path else certifi.where()
                )
                with httpx.Client(
                    base_url=self._config.board_url,
                    verify=context,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=5,
                ) as client:
                    response = client.post("/api/night/verifier", json=body)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise GrantUnavailable("invalid_authority_response")
            return result
        except (OSError, ValueError, httpx.HTTPError, subprocess.SubprocessError):
            raise GrantUnavailable("authority_unavailable") from None

    def fetch_grant(self, run_id: str, fence: str) -> ServerGrant:
        result = self.request("night.grant", run_id, {"fence": fence})
        if set(result) != {"principal", "box", "state", "remaining_seconds", "window_end_utc"}:
            raise GrantUnavailable("invalid_grant")
        if (
            not isinstance(result["principal"], str)
            or result["box"] != self._config.box
            or not isinstance(result["state"], str)
            or not _number(result["remaining_seconds"])
            or not _number(result["window_end_utc"])
            or result["remaining_seconds"] > 1200
        ):
            raise GrantUnavailable("invalid_grant")
        return ServerGrant(
            result["principal"],
            result["box"],
            result["state"],
            float(result["remaining_seconds"]),
            float(result["window_end_utc"]),
        )

    def _sign(self, envelope: bytes) -> str:
        if os.geteuid() != 0:
            raise GrantUnavailable("root_signer_required")
        path = self._config.signer_key
        if not path.is_absolute() or path.anchor != "/" or path != Path(os.path.normpath(path)):
            raise GrantUnavailable("unsafe_signer_path")
        for parent in path.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise GrantUnavailable("unsafe_signer_path")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise GrantUnavailable("unsafe_signer_path")
        result = subprocess.run(
            ["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(path), "-n", self._config.namespace],
            input=envelope,
            capture_output=True,
            timeout=5,
            check=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "SSH_ASKPASS_REQUIRE": "never"},
        )
        return result.stdout.decode()


def _number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False
