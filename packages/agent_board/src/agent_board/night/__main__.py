import argparse
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from agent_board.night import guard, server
from agent_board.night.board import SignedBoard
from agent_board.night.config import VerifierConfig, load
from agent_board.night.controller import Controller
from agent_board.night.gate import GuardState, open_gate
from agent_board.night.ledger import Ledger
from agent_board.night.sync import BoardSync
from agent_board.night.systemd import GUARD_UNIT, SYSTEMCTL, LiveCommands, SystemdRuns
from agent_board.night_authority import NightAuthorityClient
from agent_board.night_inspection import Inspector, LiveHost
from agent_board.night_state import LocalJournal

LEDGER_NAME = "ledger.sqlite3"
logger = logging.getLogger("agent_board.night")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m agent_board.night")
    parser.add_argument("command", choices=("serve", "guard", "gate"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("run_id", nargs="?")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if os.geteuid() != 0:
        logger.error("the night verifier must run as root")
        return 2
    config = load(args.config)
    journal = LocalJournal(
        config.state_path,
        box=config.box,
        principals={agent.principal: agent.uid for agent in config.agents.values()},
    )
    ledger = Ledger(config.state_path.parent / LEDGER_NAME)
    if args.command == "gate":
        if args.run_id is None:
            parser.error("gate needs a run id")
        return 0 if _gate(args.run_id, ledger, journal) else 1
    if args.command == "guard":
        _guard(config, args.config, ledger, journal)
    _serve(config, args.config, ledger, journal)
    return 0


def _serve(config: VerifierConfig, config_path: Path, ledger: Ledger, journal: LocalJournal) -> None:
    inspector = Inspector(LiveHost())
    board = SignedBoard(config.box, NightAuthorityClient(config))
    sync = BoardSync(config.box, ledger, board, inspector, journal)
    sync.reconcile()
    runs = SystemdRuns(config, config_path, ledger, inspector, LiveCommands())
    controller = Controller(config, journal, board, runs, sync)
    server.serve(server.inherited_listener(), controller)


def _guard(config: VerifierConfig, config_path: Path, ledger: Ledger, journal: LocalJournal) -> None:
    runs = SystemdRuns(config, config_path, ledger, Inspector(LiveHost()), LiveCommands())
    deadline_guard = guard.Guard(
        journal=journal,
        ledger=ledger,
        runs=runs,
        stop_grace_seconds=config.limits.stop_grace_seconds,
        invocation_id=os.environ.get("INVOCATION_ID", ""),
        boot_id=_boot_id,
        boottime=lambda: time.clock_gettime(time.CLOCK_BOOTTIME),
    )
    guard.serve(deadline_guard, guard.systemd_notify(os.environ), guard.boottime_timer())


def _gate(run_id: str, ledger: Ledger, journal: LocalJournal) -> bool:
    return open_gate(
        run_id, ledger=ledger, journal=journal, environ=os.environ, boot_id=_boot_id, guard=_guard_state
    )


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _guard_state() -> GuardState:
    try:
        shown = subprocess.run(
            [SYSTEMCTL, "--system", "show", GUARD_UNIT, "--property=ActiveState,InvocationID"],
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return GuardState(False, "")
    values = dict(line.partition("=")[::2] for line in shown.splitlines())
    return GuardState(values.get("ActiveState") == "active", values.get("InvocationID", ""))


if __name__ == "__main__":
    sys.exit(main())
