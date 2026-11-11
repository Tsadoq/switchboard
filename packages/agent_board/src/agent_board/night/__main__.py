import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from agent_board.night import client, guard, server
from agent_board.night.board import SignedBoard
from agent_board.night.config import VerifierConfig, load, trusted_file
from agent_board.night.controller import Controller
from agent_board.night.execstep import ExecRefused, LiveSystem, exec_native
from agent_board.night.gate import GuardState, open_gate
from agent_board.night.latch import latch_stop
from agent_board.night.ledger import Ledger
from agent_board.night.sync import BoardSync
from agent_board.night.systemd import SYSTEMCTL, LiveCommands, SystemdRuns
from agent_board.night_authority import NightAuthorityClient
from agent_board.night_inspection import Inspector, LiveHost
from agent_board.night_state import LocalJournal

LEDGER_NAME = "ledger.sqlite3"
SWEEP_SECONDS = 30
logger = logging.getLogger("agent_board.night")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["request"]:
        return client.main(arguments[1:])
    parser = argparse.ArgumentParser(prog="python -I -m agent_board.night")
    parser.add_argument("command", choices=("serve", "guard", "gate", "exec", "latch-stop"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--all-open", action="store_true")
    args = parser.parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not _is_root():
        logger.error("the night verifier must run as root")
        return 2
    config = load(args.config)
    if args.command == "latch-stop":
        return _latch_stop(parser, args, config)
    journal = LocalJournal(
        config.state_path,
        box=config.box,
        principals={agent.principal: agent.uid for agent in config.agents.values()},
    )
    ledger = Ledger(config.state_path.parent / LEDGER_NAME)
    if args.command in ("gate", "exec") and args.run_id is None:
        parser.error(f"{args.command} needs a run id")
    if args.command == "gate":
        return 0 if _gate(args.run_id, config.guard_unit, ledger, journal) else 1
    if args.command == "exec":
        try:
            exec_native(
                args.run_id,
                config=config,
                journal=journal,
                ledger=ledger,
                environ=os.environ,
                system=LiveSystem(_guard_state, _boot_id),
            )
        except ExecRefused as refusal:
            logger.error("night run %s will not execute: %s", args.run_id, refusal)
            return 1
    if args.command == "guard":
        _guard(config, args.config, ledger, journal)
    _serve(config, args.config, ledger, journal)
    return 0


def _serve(config: VerifierConfig, config_path: Path, ledger: Ledger, journal: LocalJournal) -> None:
    inspector = Inspector(LiveHost())
    board = SignedBoard(config.box, NightAuthorityClient(config))
    sync = BoardSync(config.box, ledger, board, inspector, journal)
    sync.reconcile()
    runs = SystemdRuns(config, config_path, ledger, inspector, LiveCommands(), _service_python())
    controller = Controller(config, journal, board, runs, sync)
    threading.Thread(target=_sweep_forever, args=(controller,), name="night-sweep", daemon=True).start()
    server.serve(server.inherited_listener(), controller)


def _sweep_forever(controller: Controller) -> None:
    while True:
        time.sleep(SWEEP_SECONDS)
        try:
            controller.sweep()
        except Exception:
            logger.exception("night sweep failed; retrying in %ds", SWEEP_SECONDS)


def _guard(config: VerifierConfig, config_path: Path, ledger: Ledger, journal: LocalJournal) -> None:
    runs = SystemdRuns(config, config_path, ledger, Inspector(LiveHost()), LiveCommands(), _service_python())
    timer = guard.BoottimeTimer()
    deadline_guard = guard.Guard(
        journal=journal,
        ledger=ledger,
        runs=runs,
        timer=timer,
        stop_grace_seconds=config.limits.stop_grace_seconds,
        invocation_id=os.environ.get("INVOCATION_ID", ""),
        boot_id=_boot_id,
        boottime=lambda: time.clock_gettime(time.CLOCK_BOOTTIME),
    )
    guard.serve(deadline_guard, timer, guard.systemd_notify(os.environ))


def _gate(run_id: str, guard_unit: str, ledger: Ledger, journal: LocalJournal) -> bool:
    return open_gate(
        run_id,
        ledger=ledger,
        journal=journal,
        environ=os.environ,
        boot_id=_boot_id,
        guard=lambda: _guard_state(guard_unit),
    )


def _service_python() -> Path:
    """The interpreter running this root service, which the root gate reuses rather than one named in config."""
    python = Path(sys.executable)
    trusted_file(python)
    return python


def _is_root() -> bool:
    return os.geteuid() == 0


def _latch_stop(parser: argparse.ArgumentParser, args: argparse.Namespace, config: VerifierConfig) -> int:
    if (args.run_id is None) == (not args.all_open):
        parser.error("latch-stop needs exactly one of a run id or --all-open")
    outcome = latch_stop(
        config,
        lambda: LocalJournal(
            config.state_path,
            box=config.box,
            principals={agent.principal: agent.uid for agent in config.agents.values()},
        ),
        LiveManager(),
        run_id=args.run_id,
        fence=None if args.all_open else sys.stdin.readline().strip(),
    )
    print(outcome.json())
    return outcome.exit_code


class LiveManager:
    def show(self, unit: str, properties: Sequence[str]) -> str:
        try:
            return subprocess.run(
                [SYSTEMCTL, "--system", "show", f"--property={','.join(properties)}", "--", unit],
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout
        except subprocess.SubprocessError as error:
            raise OSError(f"systemctl show {unit} failed: {error}") from None


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _guard_state(guard_unit: str) -> GuardState:
    try:
        shown = subprocess.run(
            [SYSTEMCTL, "--system", "show", guard_unit, "--property=ActiveState,InvocationID"],
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
