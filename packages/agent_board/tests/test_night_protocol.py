import json
from dataclasses import asdict
from uuid import uuid4

import pytest
from agent_board.runner.night_protocol import FrameError, Request, decode_request


def body() -> dict[str, object]:
    return {"v": 1, "request_id": str(uuid4()), "op": "start", "run_id": str(uuid4()), "fence": "a" * 43}


def wire(value: object) -> bytes:
    return json.dumps(value).encode() + b"\n"


@pytest.mark.parametrize("op", ["start", "status", "stop"])
def test_round_trip_and_no_fence_in_representation(op: str) -> None:
    value = body() | {"op": op}
    request = decode_request(wire(value))
    assert asdict(request) == value
    assert decode_request(request.encode()) == request
    assert str(value["fence"]) not in repr(request)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("v", True),
        ("v", 1.0),
        ("v", "1"),
        ("v", 2),
        ("op", "arm"),
        ("op", "exec"),
        ("op", None),
        ("op", ["start"]),
        ("run_id", "../ordinary"),
        ("run_id", "ABCDEFAB-1234-4ABC-9ABC-ABCDEFABCDEF"),
        ("run_id", False),
        ("request_id", ""),
        ("request_id", 1),
        ("fence", ""),
        ("fence", "a" * 31),
        ("fence", "a" * 129),
        ("fence", True),
        ("fence", "x/" + "a" * 40),
        ("command", ["/bin/sh"]),
        ("unit", "ordinary.service"),
        ("uid", 0),
        ("path", "/"),
        ("url", "https://other.invalid"),
        ("deadline", 123),
    ],
)
def test_bad_types_identity_operations_and_caller_authority_are_refused(field: str, value: object) -> None:
    with pytest.raises(FrameError):
        decode_request(wire(body() | {field: value}))


@pytest.mark.parametrize("field", ["v", "request_id", "op", "run_id", "fence"])
def test_missing_fields_are_refused(field: str) -> None:
    value = body()
    del value[field]
    with pytest.raises(FrameError):
        decode_request(wire(value))


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"{}",
        b"{}\r\n",
        b"{}\n{}\n",
        b"{}\n ",
        b"\xff\n",
        b"a" * 8192 + b"\n",
        b"[]\n",
        b"null\n",
        b'{"v":1,"v":1}\n',
        b"[" * 2000 + b"]" * 2000 + b"\n",
    ],
)
def test_malformed_multiframe_duplicate_or_oversized_input_is_refused(frame: bytes) -> None:
    with pytest.raises(FrameError):
        decode_request(frame)


def test_constructor_also_validates_before_encoding() -> None:
    with pytest.raises(FrameError):
        Request(v=1, request_id=str(uuid4()), op="start", run_id="../ordinary", fence="a" * 43)
