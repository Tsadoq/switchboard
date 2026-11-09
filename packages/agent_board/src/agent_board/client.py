import base64
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

import httpx

from agent_board import signing
from agent_board.config import Config
from agent_board.identity import Identity, ensure_state_dir, state_dir

AUTH_ACTION = "auth"
FILE_PUT_ACTION = "file.put"
TOKEN_FILE = "token.json"
_TOKEN_RENEWAL_MARGIN = timedelta(minutes=1)
# The board holds a wake poll open for up to 60 s before answering.
_TIMEOUT = httpx.Timeout(10.0, read=90.0)


class BoardError(Exception):
    def __init__(self, status: int, reason: str, detail: Mapping[str, Any] | None = None) -> None:
        self.status = status
        self.reason = reason
        self.detail = dict(detail or {})
        extra = "".join(f", {key}={value}" for key, value in self.detail.items())
        super().__init__(f"board refused: {reason} ({status}{extra})")


class BoardUnreachable(Exception):
    pass


@dataclass(frozen=True)
class ReadToken:
    value: str
    expires_at: str

    def valid_until(self, moment: datetime) -> bool:
        return datetime.fromisoformat(self.expires_at) > moment


class Client:
    def __init__(self, config: Config, identity: Identity) -> None:
        self._config = config
        self._identity = identity
        self._http = httpx.Client(base_url=config.board_url, timeout=_TIMEOUT)

    @property
    def board_url(self) -> str:
        return self._config.board_url

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def signed(self, action: str, target: str, payload: dict[str, Any]) -> Any:
        return self._json("POST", "/api/signed", json=_wire(*self._signed_pair(action, target, payload)))

    def signed_upload(self, declaration: dict[str, Any], path: Path) -> Any:
        fields = _wire(*self._signed_pair(FILE_PUT_ACTION, "", declaration))
        with path.open("rb") as blob:
            return self._json(
                "POST",
                "/api/files",
                data=fields,
                files={"blob": (path.name, blob, "application/octet-stream")},
            )

    def token(self, *, refresh: bool = False) -> ReadToken:
        cached = None if refresh else self._cached_token()
        return cached or self._login()

    def get(self, path: str, **params: Any) -> Any:
        with self._authorized("GET", path, params=params) as response:
            return _parsed(response)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        with self._authorized("POST", path, json=body) as response:
            return _parsed(response)

    def download(self, path: str, sink: BinaryIO) -> None:
        with self._authorized("GET", path) as response:
            if not response.is_success:
                _parsed(response)
            sink.writelines(response.iter_bytes())

    def _signed_pair(self, action: str, target: str, payload: dict[str, Any]) -> tuple[bytes, str]:
        data = signing.canonical(signing.envelope(self._identity, action, target, payload))
        return data, signing.sign(data, self._identity.kind)

    def _login(self) -> ReadToken:
        issued = self._json("POST", "/api/auth/challenge", json={"principal": self._identity.principal})
        pair = self._signed_pair(AUTH_ACTION, "", {"challenge": issued["challenge"]})
        redeemed = self._json("POST", "/api/auth/token", json=_wire(*pair))
        token = ReadToken(redeemed["token"], redeemed["expires_at"])
        self._store_token(token)
        return token

    def _cached_token(self) -> ReadToken | None:
        try:
            cached = json.loads((state_dir(self._identity.kind) / TOKEN_FILE).read_text())
            token = ReadToken(cached["token"], cached["expires_at"])
            usable = cached["board_url"] == self._config.board_url and token.valid_until(
                datetime.now(UTC) + _TOKEN_RENEWAL_MARGIN
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return token if usable else None

    def _store_token(self, token: ReadToken) -> None:
        directory = ensure_state_dir(self._identity.kind)
        record = {"board_url": self._config.board_url, "token": token.value, "expires_at": token.expires_at}
        # The runner and the agent's own CLI share this file, so it is replaced, never rewritten in place.
        with tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=f".{TOKEN_FILE}.", delete=False
        ) as staged:
            json.dump(record, staged)
        os.replace(staged.name, directory / TOKEN_FILE)

    def _json(self, method: str, path: str, **request: Any) -> Any:
        with self._exchange(method, path, **request) as response:
            return _parsed(response)

    @contextmanager
    def _authorized(self, method: str, path: str, **request: Any) -> Iterator[httpx.Response]:
        with self._exchange(method, path, headers=_bearer(self.token()), **request) as response:
            if response.status_code != httpx.codes.UNAUTHORIZED:
                yield response
                return
        with self._exchange(method, path, headers=_bearer(self.token(refresh=True)), **request) as response:
            yield response

    @contextmanager
    def _exchange(self, method: str, path: str, **request: Any) -> Iterator[httpx.Response]:
        try:
            with self._http.stream(method, path, **request) as response:
                yield response
        except httpx.TransportError as error:
            raise BoardUnreachable(f"cannot reach the board at {self._config.board_url}: {error}") from None


def _wire(envelope_bytes: bytes, signature: str) -> dict[str, str]:
    return {"envelope": base64.urlsafe_b64encode(envelope_bytes).decode(), "signature": signature}


def _bearer(token: ReadToken) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.value}"}


def _parsed(response: httpx.Response) -> Any:
    response.read()
    body = _json_or_none(response)
    if response.is_success:
        return body
    if isinstance(body, dict) and isinstance(body.get("reason"), str):
        detail = {key: value for key, value in body.items() if key != "reason"}
        raise BoardError(response.status_code, body["reason"], detail)
    raise BoardError(response.status_code, response.reason_phrase or "error")


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
