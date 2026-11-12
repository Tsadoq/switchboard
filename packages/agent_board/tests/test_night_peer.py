import array
import os
import socket
import subprocess
import sys
from collections.abc import Iterator

import pytest
from agent_board.night.peer import (
    MAX_FRAME_BYTES,
    SO_PASSPIDFD,
    PeerRefused,
    enable_peer_credentials,
    receive_frame,
    sender_cgroup,
)

SEND_AND_WAIT = (
    "import socket, sys\n"
    "s = socket.socket(fileno=int(sys.argv[1]))\n"
    "s.send(sys.argv[2].encode() * int(sys.argv[3]))\n"
    "sys.stdin.read()\n"
)
SEND_AND_EXIT = "import socket, sys\nsocket.socket(fileno=int(sys.argv[1])).send(b'frame')\n"
SEND_WITH_RIGHTS = (
    "import array, os, socket, sys\n"
    "s = socket.socket(fileno=int(sys.argv[1]))\n"
    "r, w = os.pipe()\n"
    "s.sendmsg([b'frame'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [r]).tobytes())])\n"
    "sys.stdin.read()\n"
)
RECEIVE_TRANSFERRED_THEN_SEND = (
    "import array, socket, sys\n"
    "carrier = socket.socket(fileno=int(sys.argv[1]))\n"
    "_, ancillary, _, _ = carrier.recvmsg(1, socket.CMSG_SPACE(4))\n"
    "fd = array.array('i', ancillary[0][2][:4])[0]\n"
    "socket.socket(fileno=fd).send(b'frame')\n"
    "sys.stdin.read()\n"
)


def _open_fds() -> set[int]:
    return {int(name) for name in os.listdir("/proc/self/fd")}


@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        yield receiver, sender
    finally:
        receiver.close()
        sender.close()


def _spawn(code: str, fd: int, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(fd), *args],
        pass_fds=[fd],
        stdin=subprocess.PIPE,
        text=True,
    )


def _finish(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        process.stdin.close()
    process.wait(timeout=10)


def _receive(receiver: socket.socket) -> object:
    return receive_frame(receiver, expected_uid=os.getuid(), expected_gid=os.getgid())


def test_a_live_sender_is_identified_by_kernel_credentials_and_a_close_on_exec_pidfd(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    receiver, sender = pair
    enable_peer_credentials(receiver)
    process = _spawn(SEND_AND_WAIT, sender.fileno(), "frame", "1")
    try:
        frame = receive_frame(receiver, expected_uid=os.getuid(), expected_gid=os.getgid())
        try:
            assert frame.payload == b"frame"
            assert (frame.pid, frame.uid, frame.gid) == (process.pid, os.getuid(), os.getgid())
            assert not os.get_inheritable(frame.pidfd), "the pidfd was not received close-on-exec"
            with open(f"/proc/{process.pid}/cgroup") as expected:
                assert sender_cgroup(frame) == expected.read().strip()
        finally:
            frame.close()
    finally:
        _finish(process)


def test_a_sender_that_exited_but_is_not_reaped_is_refused(pair: tuple[socket.socket, socket.socket]) -> None:
    receiver, sender = pair
    enable_peer_credentials(receiver)
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", SEND_AND_EXIT, str(sender.fileno())], pass_fds=[sender.fileno()]
    )
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    before = _open_fds()
    try:
        with pytest.raises(PeerRefused, match="sender_exited"):
            _receive(receiver)
        assert _open_fds() == before, "a refused frame leaked its pidfd"
    finally:
        process.wait(timeout=10)


def test_a_reaped_sender_is_refused(pair: tuple[socket.socket, socket.socket]) -> None:
    receiver, sender = pair
    enable_peer_credentials(receiver)
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", SEND_AND_EXIT, str(sender.fileno())], pass_fds=[sender.fileno()]
    )
    process.wait(timeout=10)
    before = _open_fds()

    with pytest.raises(PeerRefused, match="sender_exited"):
        _receive(receiver)

    assert _open_fds() == before


def test_a_transferred_socket_authenticates_whoever_sends_on_it(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    receiver, endpoint = pair
    enable_peer_credentials(receiver)
    carrier, carried = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    outsider = _spawn(RECEIVE_TRANSFERRED_THEN_SEND, carried.fileno())
    carried.close()
    try:
        carrier.sendmsg(
            [b"x"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [endpoint.fileno()]).tobytes())]
        )
        endpoint.close()
        frame = receive_frame(receiver, expected_uid=os.getuid(), expected_gid=os.getgid())
        try:
            assert frame.pid == outsider.pid, (
                "the frame was attributed to the socket's creator, not its sender"
            )
            assert frame.pid != os.getpid()
        finally:
            frame.close()
    finally:
        carrier.close()
        _finish(outsider)


def test_a_frame_carrying_descriptors_is_refused_and_they_are_closed(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    receiver, sender = pair
    enable_peer_credentials(receiver)
    process = _spawn(SEND_WITH_RIGHTS, sender.fileno())
    before = _open_fds()
    try:
        with pytest.raises(PeerRefused, match="unexpected_ancillary_data"):
            _receive(receiver)
        assert _open_fds() == before, "a refused frame leaked a received descriptor"
    finally:
        _finish(process)


def test_an_oversized_frame_is_refused_as_truncated(pair: tuple[socket.socket, socket.socket]) -> None:
    receiver, sender = pair
    enable_peer_credentials(receiver)
    process = _spawn(SEND_AND_WAIT, sender.fileno(), "x", str(MAX_FRAME_BYTES + 1))
    before = _open_fds()
    try:
        with pytest.raises(PeerRefused, match="frame_truncated"):
            _receive(receiver)
        assert _open_fds() == before
    finally:
        _finish(process)


def test_truncated_ancillary_data_is_refused_and_cleaned_up(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    receiver, sender = pair
    enable_peer_credentials(receiver)
    process = _spawn(SEND_AND_WAIT, sender.fileno(), "frame", "1")
    before = _open_fds()
    try:
        with pytest.raises(PeerRefused, match="frame_truncated"):
            receive_frame(
                receiver,
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
                ancillary_bytes=socket.CMSG_SPACE(4),
            )
        assert _open_fds() == before
    finally:
        _finish(process)


def test_a_frame_without_kernel_credentials_is_refused(pair: tuple[socket.socket, socket.socket]) -> None:
    receiver, sender = pair
    receiver.setsockopt(socket.SOL_SOCKET, SO_PASSPIDFD, 1)
    process = _spawn(SEND_AND_WAIT, sender.fileno(), "frame", "1")
    try:
        with pytest.raises(PeerRefused, match="unexpected_ancillary_data"):
            _receive(receiver)
    finally:
        _finish(process)


@pytest.mark.parametrize(("uid_delta", "gid_delta"), [(1, 0), (0, 1)])
def test_a_sender_with_another_identity_is_refused(
    pair: tuple[socket.socket, socket.socket], uid_delta: int, gid_delta: int
) -> None:
    receiver, sender = pair
    enable_peer_credentials(receiver)
    process = _spawn(SEND_AND_WAIT, sender.fileno(), "frame", "1")
    try:
        with pytest.raises(PeerRefused, match="unexpected_sender_identity"):
            receive_frame(
                receiver, expected_uid=os.getuid() + uid_delta, expected_gid=os.getgid() + gid_delta
            )
    finally:
        _finish(process)


class NoPidfdSocket(socket.socket):
    def setsockopt(self, *args: object) -> None:
        raise OSError(92, "Protocol not available")


def test_a_kernel_without_peer_pidfds_refuses_instead_of_falling_back() -> None:
    plain, other = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver = NoPidfdSocket(socket.AF_UNIX, socket.SOCK_SEQPACKET, fileno=plain.detach())
    try:
        with pytest.raises(PeerRefused, match="peer_credentials_unavailable"):
            enable_peer_credentials(receiver)
    finally:
        receiver.close()
        other.close()


def test_only_a_seqpacket_socket_is_accepted() -> None:
    stream, other = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(PeerRefused, match="not_a_seqpacket_socket"):
            enable_peer_credentials(stream)
    finally:
        stream.close()
        other.close()
