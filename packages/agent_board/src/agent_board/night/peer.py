import array
import os
import select
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

SO_PASSCRED = getattr(socket, "SO_PASSCRED", 16)
SO_PASSPIDFD = getattr(socket, "SO_PASSPIDFD", 76)
SCM_CREDENTIALS = getattr(socket, "SCM_CREDENTIALS", 2)
SCM_PIDFD = getattr(socket, "SCM_PIDFD", 4)
SCM_RIGHTS = socket.SCM_RIGHTS
MSG_CMSG_CLOEXEC = getattr(socket, "MSG_CMSG_CLOEXEC", 0x40000000)
MAX_FRAME_BYTES = 512
_UCRED = struct.Struct("3i")
_INT = struct.calcsize("i")
_ANCILLARY_BYTES = socket.CMSG_SPACE(_UCRED.size) + socket.CMSG_SPACE(_INT) + socket.CMSG_SPACE(_INT * 8)


class PeerRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class PeerFrame:
    """One frame and the kernel's account of the process that sent it; the caller owns `pidfd`."""

    payload: bytes
    pid: int
    uid: int
    gid: int
    pidfd: int

    def close(self) -> None:
        os.close(self.pidfd)


def enable_peer_credentials(sock: socket.socket) -> None:
    """Asks the kernel to attach the sender's credentials and a pidfd to every message; refuses if it cannot."""
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_SEQPACKET:
        raise PeerRefused("not_a_seqpacket_socket")
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_PASSCRED, 1)
        sock.setsockopt(socket.SOL_SOCKET, SO_PASSPIDFD, 1)
    except OSError:
        raise PeerRefused("peer_credentials_unavailable") from None


def receive_frame(
    sock: socket.socket,
    *,
    expected_uid: int,
    expected_gid: int,
    ancillary_bytes: int = _ANCILLARY_BYTES,
) -> PeerFrame:
    """Exactly one frame carrying exactly one kernel credential and one pidfd that agree with each other and
    with the expected identity, from a sender still alive. Every descriptor received is closed on refusal."""
    payload, ancillary, flags, _ = sock.recvmsg(MAX_FRAME_BYTES, ancillary_bytes, MSG_CMSG_CLOEXEC)
    received = _descriptors(ancillary)
    try:
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise PeerRefused("frame_truncated")
        if not payload:
            raise PeerRefused("empty_frame")
        kinds = sorted((level, kind) for level, kind, _ in ancillary)
        if kinds != sorted([(socket.SOL_SOCKET, SCM_CREDENTIALS), (socket.SOL_SOCKET, SCM_PIDFD)]):
            raise PeerRefused("unexpected_ancillary_data")
        credentials = next(data for level, kind, data in ancillary if kind == SCM_CREDENTIALS)
        pidfd_data = next(data for level, kind, data in ancillary if kind == SCM_PIDFD)
        if len(credentials) != _UCRED.size or len(pidfd_data) != _INT:
            raise PeerRefused("unexpected_ancillary_data")
        pid, uid, gid = _UCRED.unpack(credentials)
        (pidfd,) = struct.unpack("i", pidfd_data)
        if pidfd < 0:
            raise PeerRefused("sender_exited")
        if uid != expected_uid or gid != expected_gid:
            raise PeerRefused("unexpected_sender_identity")
        pidfd_owner = pidfd_pid(pidfd)
        if pidfd_owner == -1 or not alive(pidfd):
            raise PeerRefused("sender_exited")
        if pidfd_owner != pid or pid <= 0:
            raise PeerRefused("credentials_disagree_with_pidfd")
    except BaseException:
        for fd in received:
            os.close(fd)
        raise
    for fd in received:
        if fd != pidfd:
            os.close(fd)
    return PeerFrame(payload, pid, uid, gid, pidfd)


def sender_cgroup(frame: PeerFrame, proc: Path = Path("/proc")) -> str:
    """The sender's cgroup, read only while its pidfd shows it alive before and after, so a reused pid
    number cannot answer for it."""
    if not alive(frame.pidfd):
        raise PeerRefused("sender_exited")
    try:
        cgroup = (proc / str(frame.pid) / "cgroup").read_text().strip()
    except OSError:
        raise PeerRefused("sender_cgroup_unreadable") from None
    if not alive(frame.pidfd) or pidfd_pid(frame.pidfd) != frame.pid:
        raise PeerRefused("sender_exited")
    return cgroup


def alive(pidfd: int) -> bool:
    """A pidfd polls readable once its process has exited, reaped or not; its pid alone does not show that."""
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return not poller.poll(0)


def pidfd_pid(pidfd: int) -> int:
    try:
        with open(f"/proc/self/fdinfo/{pidfd}") as info:
            for line in info:
                if line.startswith("Pid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    raise PeerRefused("pidfd_unreadable")


def _descriptors(ancillary: list[tuple[int, int, bytes]]) -> list[int]:
    """Received descriptors to close. A sender already reaped arrives as a negative errno on some kernels and as
    a pidfd whose fdinfo pid is -1 on others; both mean the sender exited."""
    fds: list[int] = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind in (SCM_PIDFD, SCM_RIGHTS):
            usable = len(data) - len(data) % _INT
            fds.extend(fd for fd in array.array("i", data[:usable]) if fd >= 0)
    return fds
