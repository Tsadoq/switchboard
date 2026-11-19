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
    """Requests stop for a named fenced run, or for every run the legacy journal lists as open.

    The arguments and the configured verifier socket and service are checked before the journal is opened,
    because opening it can already create, refresh or recover legacy state. No other unit is queried, and
    no stop, kill, mask or unmask is issued. The verifier is not the only journal writer: the guard, the
    controller's reserve and claim_start, gate and exec journal construction, root latch paths and expiry
    refresh can all write. A point-in-time unit observation is not all-writer exclusion and does not stop a
    later start; the caller must hold no-new-admission and no-new-start ordering over the whole call. The
    guard must not be required stopped first: run units bind to it, so stopping it can end runs before
    their stop request is persisted.

    The latch itself never marks a run stopped and never releases a claimed run, which stays stopping or
    quarantined until separate settlement. The journal constructor, active() and each latch first run the
    expiry refresh, which stops an expired or boot-changed never-claimed, non-uncertain reservation (so it
    leaves the active slot) and quarantines an expired claimed or uncertain run. So `already` only means
    this call did not change the latch, `no_run` only describes the legacy journal view, and enumeration
    and each per-run latch are separate transactions. A refusal after open does not mean the files are
    unchanged. None of these results is a physical-stop, settlement, all-writer-absence or
    universal-emptiness certificate.
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
    """None only when the observation shows the unit stopped at that instant.

    Every property must appear exactly once, LoadState must be loaded or not-found (masked is refused),
    ActiveState inactive or failed, no job, and each cgroup empty or gone. It does not exclude a later
    start, a process outside the unit's cgroups or any other writer, and it is not a maintenance proof.
    """
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
