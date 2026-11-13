import argparse
import array
import errno
import os
import socket
import subprocess
import sys
from pathlib import Path

from agent_board.night import peer


def _inventory() -> dict[int, tuple[int, int, str]]:
    found = {}
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        try:
            metadata = os.fstat(fd)
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.EBADF):
                raise
            continue
        found[fd] = (metadata.st_dev, metadata.st_ino, target)
    return found


def _sender(endpoint: int, writer: int) -> None:
    with socket.socket(fileno=endpoint) as sender:
        sender.sendmsg(
            [b"frame"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [writer]).tobytes())]
        )
        os.close(writer)
        os.write(1, b"R")
        sys.stdin.buffer.read()


def _prove(leak: str, close_unrelated: bool) -> None:
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    reader, writer = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    writer_owned: int | None = writer
    unrelated: list[int] = []
    retained: list[int] = []
    process: subprocess.Popen[bytes] | None = None
    original = peer._descriptors

    def descriptors(ancillary: list[tuple[int, int, bytes]]) -> list[int]:
        received = original(ancillary)
        wanted = {"rights": socket.SCM_RIGHTS, "pidfd": peer.SCM_PIDFD}.get(leak)
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == wanted:
                usable = len(data) - len(data) % array.array("i").itemsize
                retained.extend(fd for fd in array.array("i", data[:usable]) if fd >= 0)
        return [fd for fd in received if fd not in retained]

    try:
        peer.enable_peer_credentials(receiver)
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--sender",
                str(sender.fileno()),
                str(writer),
            ],
            pass_fds=(sender.fileno(), writer),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        os.close(writer)
        writer_owned = None
        assert process.stdout is not None and process.stdout.read(1) == b"R", "sender_not_ready"
        if close_unrelated:
            unrelated.extend(os.pipe2(os.O_CLOEXEC))
        before = _inventory()
        for fd in unrelated:
            os.close(fd)
        unrelated.clear()
        peer._descriptors = descriptors
        try:
            frame = peer.receive_frame(receiver, expected_uid=os.getuid(), expected_gid=os.getgid())
        except peer.PeerRefused as refused:
            assert str(refused) == "unexpected_ancillary_data", str(refused)
        else:
            frame.close()
            raise AssertionError("frame_not_refused")
        if leak != "none":
            assert retained, "leak_control_received_no_target_descriptor"
            if close_unrelated:
                assert all(fd in before for fd in retained), "leak_control_did_not_reuse_baseline_numbers"
        try:
            assert os.read(reader, 1) == b"", "unexpected_pipe_data"
        except BlockingIOError as exc:
            raise AssertionError("received_rights_not_closed") from exc
        after = _inventory()
        unexpected = {fd: identity for fd, identity in after.items() if before.get(fd) != identity}
        assert not unexpected, f"received_descriptor_not_closed: {unexpected}"
        removed = set(before) - set(after)
        assert len(removed) == (2 if close_unrelated else 0), f"unexpected_baseline_change: {removed}"
        print(f"proof_ok removed={len(removed)}")
    finally:
        peer._descriptors = original
        for fd in retained:
            os.close(fd)
        for fd in unrelated:
            os.close(fd)
        if writer_owned is not None:
            os.close(writer_owned)
        os.close(reader)
        receiver.close()
        sender.close()
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--sender":
        _sender(int(sys.argv[2]), int(sys.argv[3]))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--leak", choices=("none", "rights", "pidfd"), default="none")
    parser.add_argument("--close-unrelated", action="store_true")
    args = parser.parse_args()
    _prove(args.leak, args.close_unrelated)


if __name__ == "__main__":
    main()
