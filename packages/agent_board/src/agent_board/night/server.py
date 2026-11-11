import json
import logging
import os
import socket
import struct
from typing import NoReturn, Protocol

from agent_board.night.controller import Response
from agent_board.runner.night_protocol import MAX_FRAME_BYTES, FrameError, Request, decode_request

SYSTEMD_FIRST_FD = 3
CONNECTION_TIMEOUT_SECONDS = 5
_UCRED = struct.Struct("3i")
logger = logging.getLogger(__name__)


class Handler(Protocol):
    def handle(self, request: Request, peer_uid: int) -> Response: ...


class SocketError(RuntimeError):
    pass


def inherited_listener(environ: dict[str, str] | None = None) -> socket.socket:
    env = os.environ if environ is None else environ
    if env.get("LISTEN_PID") != str(os.getpid()) or env.get("LISTEN_FDS") != "1":
        raise SocketError("expected exactly one socket from systemd (LISTEN_PID/LISTEN_FDS)")
    listener = socket.socket(fileno=SYSTEMD_FIRST_FD)
    if listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM:
        listener.detach()
        raise SocketError("the inherited socket is not a unix stream socket")
    return listener


def serve(listener: socket.socket, handler: Handler) -> NoReturn:
    while True:
        connection, _ = listener.accept()
        with connection:
            try:
                answer_one(connection, handler)
            except Exception:
                logger.exception("night verifier connection failed")


def answer_one(connection: socket.socket, handler: Handler) -> None:
    connection.settimeout(CONNECTION_TIMEOUT_SECONDS)
    uid = peer_uid(connection)
    try:
        request = decode_request(_read_frame(connection))
    except (FrameError, OSError) as error:
        _send(connection, {"v": 1, "status": "refused", "reason": _frame_reason(error)})
        return
    _send(connection, handler.handle(request, uid).as_json())


def peer_uid(connection: socket.socket) -> int:
    _pid, uid, _gid = _UCRED.unpack(connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size))
    return int(uid)


def _read_frame(connection: socket.socket) -> bytes:
    frame = b""
    while not frame.endswith(b"\n") and len(frame) <= MAX_FRAME_BYTES:
        chunk = connection.recv(MAX_FRAME_BYTES + 1 - len(frame))
        if not chunk:
            break
        frame += chunk
    return frame


def _send(connection: socket.socket, body: dict[str, object]) -> None:
    connection.sendall(json.dumps(body, separators=(",", ":")).encode() + b"\n")


def _frame_reason(error: Exception) -> str:
    return str(error) if isinstance(error, FrameError) else "connection_error"
