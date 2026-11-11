import os
import signal
import sys
import time
from collections.abc import Sequence
from typing import NoReturn

SLEEP_SECONDS = 3600


def main(argv: Sequence[str]) -> int:
    """A stand-in for the native launcher during managed acceptance runs.

    It ignores SIGTERM and leaves a setsid child behind, the two things a real agent might do, so the
    acceptance run can show that the gate, stop and cgroup.kill still empty the run's cgroup. It writes
    nothing and contacts no network.
    """
    if len(argv) != 1:
        return 2
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.fork() == 0:
        os.setsid()
        _sleep_forever()
    _sleep_forever()


def _sleep_forever() -> NoReturn:
    while True:
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
