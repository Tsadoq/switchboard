import errno
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agent_board.night.config import VerifierConfig
from agent_board.night_inspection import CGROUP_ROOT
from agent_board.night_state import LocalJournal, StateError

LATCHED_EXIT = 0
REFUSED_EXIT = 1
_PROPERTIES = ("LoadState", "ActiveState", "Job", "ControlGroup")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Manager(Protocol):
    def show(self, unit: str, properties: Sequence[str]) -> str:
        """`systemctl show --property=…` output; raises OSError when the manager cannot answer."""
        ...


@dataclass
class Outcome:
    latched: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    no_run: bool = False
    refused: str | None = None

    def json(self) -> str:
        return json.dumps(
            {
                "latched": self.latched,
                "already": self.already,
                "no_run": self.no_run,
                "refused": self.refused,
            },
            sort_keys=True,
        )

    @property
    def exit_code(self) -> int:
        return REFUSED_EXIT if self.refused is not None else LATCHED_EXIT


def latch_stop(
    config: VerifierConfig,
    open_journal: Callable[[], LocalJournal],
    manager: Manager,
    *,
    run_id: str | None,
    fence: str | None,
    events: Callable[[Path], str] = lambda path: path.read_text(),
) -> Outcome:
    """Durably requests stop for night runs once the only journal writer, the verifier, is proven gone.

    Arguments are validated and the verifier proven stopped before the journal is even opened, since opening
    it already refreshes expiry state. It only latches: no run is marked stopped, no slot is released.
    A named run needs its fence; --all-open (run_id None) latches every open run on root's authority alone,
    which is sound only because no request writer can still be adding or starting runs.
    """
    if run_id is not None and (_UUID.fullmatch(run_id) is None or not fence):
        return Outcome(refused="invalid_run")
    for unit in (config.verifier_socket, config.verifier_unit):
        why = unit_not_stopped(manager, unit, events)
        if why is not None:
            return Outcome(refused=f"{unit}: {why}")
    try:
        journal = open_journal()
        if run_id is None or fence is None:
            return _latch_all(journal)
        record, changed = journal.latch_stop(run_id, fence)
    except StateError as error:
        reason = str(error)
        return Outcome(refused="unknown_run_or_wrong_fence" if reason == "stale_fence" else reason)
    return Outcome(latched=[run_id]) if changed else Outcome(already=[record.run_id])


def _latch_all(journal: LocalJournal) -> Outcome:
    outcome = Outcome()
    open_runs = journal.active()
    for record in open_runs:
        _, changed = journal.latch_stop(record.run_id, record.fence)
        (outcome.latched if changed else outcome.already).append(record.run_id)
    outcome.no_run = not open_runs
    return outcome


def unit_not_stopped(manager: Manager, unit: str, events: Callable[[Path], str]) -> str | None:
    """None only on a positive stop proof: every property exactly once, inactive, no job, cgroups empty or gone."""
    try:
        shown = manager.show(unit, _PROPERTIES)
    except OSError as error:
        return f"manager unreadable: {error}"
    values: dict[str, str] = {}
    for name in _PROPERTIES:
        lines = [line for line in shown.splitlines() if line.startswith(f"{name}=")]
        if len(lines) != 1:
            return f"{name} appears {len(lines)} times"
        values[name] = lines[0].partition("=")[2]
    if values["LoadState"] not in ("loaded", "not-found"):
        return f"LoadState={values['LoadState']}"
    if values["ActiveState"] not in ("inactive", "failed"):
        return f"ActiveState={values['ActiveState']}"
    if values["Job"] != "":
        return f"Job={values['Job']}"
    for cgroup in {values["ControlGroup"], f"/system.slice/{unit}"} - {""}:
        why = _cgroup_not_empty(CGROUP_ROOT / cgroup.lstrip("/"), events)
        if why is not None:
            return why
    return None


def _cgroup_not_empty(group: Path, events: Callable[[Path], str]) -> str | None:
    try:
        text = events(group / "cgroup.events")
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        return f"cgroup.events unreadable: {group}"
    if "populated 0" not in text.splitlines():
        return f"cgroup populated or unknown: {group}"
    return None
