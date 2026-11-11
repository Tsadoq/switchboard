import logging
import os
import select
import socket
import time
from collections.abc import Callable, Mapping
from typing import NoReturn, Protocol

from agent_board.night.controller import unit_name
from agent_board.night.ledger import Arm, Ledger
from agent_board.night_state import LocalJournal, Record, StateError

POLL_SECONDS = 1.0
logger = logging.getLogger(__name__)


class Stopper(Protocol):
    def stop(self, unit: str) -> None: ...


class Guard:
    """The root deadline guard for this box's night runs, independent of the request controller.

    It only ever enforces the absolute deadlines the journal stored when the run was reserved; restarting it
    re-arms those same deadlines and never grants a fresh budget.
    """

    def __init__(
        self,
        *,
        journal: LocalJournal,
        ledger: Ledger,
        runs: Stopper,
        stop_grace_seconds: int,
        invocation_id: str,
        boot_id: Callable[[], str],
        boottime: Callable[[], float],
        utc: Callable[[], float] = time.time,
    ) -> None:
        self._journal = journal
        self._ledger = ledger
        self._runs = runs
        self._grace = stop_grace_seconds
        self._invocation_id = invocation_id
        self._boot_id = boot_id
        self._boottime = boottime
        self._utc = utc
        self._stopped: set[str] = set()

    def tick(self) -> None:
        boot = self._boot_id()
        for record in self._journal.active():
            if record.boot_id == boot and record.state in ("reserved", "starting"):
                self._ledger.arm(
                    Arm(
                        record.run_id,
                        boot,
                        self._invocation_id,
                        record.deadline_boottime,
                        record.deadline_utc,
                    )
                )
            if record.launch_claimed and self._due(record, boot):
                self._stop(record)

    def next_wake(self) -> float:
        """The boot-time clock reading at which the guard must look again."""
        deadlines = [
            record.deadline_boottime - self._grace
            for record in self._journal.active()
            if record.launch_claimed and record.run_id not in self._stopped
        ]
        return min([self._boottime() + POLL_SECONDS, *deadlines])

    def _due(self, record: Record, boot: str) -> bool:
        return (
            record.boot_id != boot
            or record.state in ("stopping", "quarantined")
            or self._boottime() >= record.deadline_boottime - self._grace
            or self._utc() >= record.deadline_utc - self._grace
        )

    def _stop(self, record: Record) -> None:
        if record.run_id in self._stopped:
            return
        logger.warning("stopping night run %s at its deadline (state %s)", record.run_id, record.state)
        try:
            self._runs.stop(unit_name(record.run_id))
        finally:
            self._stopped.add(record.run_id)
            try:
                self._journal.request_stop(record.run_id, record.fence)
            except StateError as error:
                logger.error("could not record the stop of %s: %s", record.run_id, error)


def serve(guard: Guard, notify: Callable[[str], None], wait_until: Callable[[float], None]) -> NoReturn:
    guard.tick()
    notify("READY=1")
    while True:
        wait_until(guard.next_wake())
        guard.tick()


def boottime_timer() -> Callable[[float], None]:
    timer = os.timerfd_create(time.CLOCK_BOOTTIME, flags=os.TFD_CLOEXEC)

    def wait_until(deadline: float) -> None:
        os.timerfd_settime(timer, flags=os.TFD_TIMER_ABSTIME, initial=max(deadline, 1e-9))
        select.select([timer], [], [])
        os.read(timer, 8)

    return wait_until


def systemd_notify(environ: Mapping[str, str]) -> Callable[[str], None]:
    address = environ.get("NOTIFY_SOCKET", "")
    if not address:
        raise RuntimeError("the guard must run as a Type=notify unit (NOTIFY_SOCKET is unset)")
    target = "\0" + address[1:] if address.startswith("@") else address

    def notify(message: str) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sender:
            sender.sendto(message.encode(), target)

    return notify
