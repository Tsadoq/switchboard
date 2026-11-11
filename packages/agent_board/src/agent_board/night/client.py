import argparse
import json
import socket
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from agent_board.night_protocol import MAX_FRAME_BYTES, FrameError, Operation, Request

TIMEOUT_SECONDS = 60
REFUSED_EXIT = 1
USAGE_EXIT = 2


def request(socket_path: Path, op: Operation, run_id: str, fence: str) -> dict[str, object]:
    frame = Request(v=1, request_id=str(uuid4()), op=op, run_id=run_id, fence=fence).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(TIMEOUT_SECONDS)
        connection.connect(str(socket_path))
        connection.sendall(frame)
        reply = b""
        while not reply.endswith(b"\n") and len(reply) <= MAX_FRAME_BYTES:
            chunk = connection.recv(MAX_FRAME_BYTES + 1 - len(reply))
            if not chunk:
                break
            reply += chunk
    answer = json.loads(reply)
    if not isinstance(answer, dict):
        raise TypeError("the verifier answered with something other than an object")
    return answer


def main(argv: Sequence[str], stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    """Sends one start, status or stop frame as the calling uid; the fence is read from stdin, not argv."""
    parser = argparse.ArgumentParser(prog="python -I -m agent_board.night request")
    parser.add_argument("op", choices=("start", "status", "stop"))
    parser.add_argument("run_id")
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args(argv)
    fence = stdin.readline().strip()
    try:
        answer = request(args.socket, args.op, args.run_id, fence)
    except FrameError as error:
        print(f"invalid request: {error}", file=sys.stderr)
        return USAGE_EXIT
    except (OSError, ValueError, TypeError) as error:
        print(f"no answer from the verifier: {error}", file=sys.stderr)
        return USAGE_EXIT
    print(json.dumps(answer, sort_keys=True), file=stdout)
    return REFUSED_EXIT if answer.get("status") == "refused" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
