import json
import re
from dataclasses import asdict, dataclass, field
from typing import Literal, NoReturn
from uuid import UUID

MAX_FRAME_BYTES = 8192
_FIELDS = frozenset(("v", "request_id", "op", "run_id", "fence"))

type Operation = Literal["start", "status", "stop"]


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    v: int
    request_id: str
    op: Operation
    run_id: str
    fence: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.v) is not int or self.v != 1:
            raise FrameError("unsupported_version")
        if not isinstance(self.op, str) or self.op not in ("start", "status", "stop"):
            raise FrameError("invalid_operation")
        if not _uuid(self.request_id) or not _uuid(self.run_id):
            raise FrameError("invalid_identifier")
        if not isinstance(self.fence, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.fence) is None:
            raise FrameError("invalid_fence")

    def encode(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode() + b"\n"


def decode_request(frame: bytes) -> Request:
    if (
        not 0 < len(frame) <= MAX_FRAME_BYTES
        or not frame.endswith(b"\n")
        or frame.count(b"\n") != 1
        or b"\r" in frame
    ):
        raise FrameError("invalid_frame")
    try:
        value = json.loads(frame.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise FrameError("invalid_json") from None
    if not isinstance(value, dict) or frozenset(value) != _FIELDS:
        raise FrameError("invalid_fields")
    return Request(**value)


def _uuid(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FrameError("duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    raise FrameError("invalid_constant")
