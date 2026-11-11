import logging
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from agent_board.night.config import Agent, VerifierConfig
from agent_board.night_protocol import Request
from agent_board.night_state import (
    MIN_START_SECONDS,
    ClockReading,
    Grant,
    LocalJournal,
    Record,
    StateError,
    clock_reading,
)

ADMISSIBLE_STATES = ("admitted", "running")
logger = logging.getLogger(__name__)

type Status = Literal["starting", "running", "stopping", "stopped", "quarantined", "refused"]


@dataclass(frozen=True)
class ServerGrant:
    principal: str
    box: str
    state: str
    remaining_seconds: float
    window_end_utc: float


class GrantUnavailable(RuntimeError):
    pass


class RunManagerError(RuntimeError):
    pass


_MANAGER_FAILURES = (RunManagerError, OSError, subprocess.SubprocessError)


class GrantSource(Protocol):
    def fetch_grant(self, run_id: str, fence: str) -> ServerGrant:
        """Raises GrantUnavailable when the board cannot vouch for the run right now."""
        ...


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    unit: str
    agent: Agent
    runtime_max_seconds: float


@dataclass(frozen=True)
class Observation:
    unit_active: bool
    cgroup_populated: bool | None


class RunManager(Protocol):
    """Implementations raise RunManagerError, OSError or a subprocess error when an action's outcome is unknown."""

    def launch(self, spec: RunSpec) -> None: ...

    def stop(self, unit: str) -> None: ...

    def inspect(self, unit: str) -> Observation: ...


@dataclass(frozen=True)
class Response:
    request_id: str
    run_id: str
    status: Status
    reason: str | None = None

    def as_json(self) -> dict[str, object]:
        body: dict[str, object] = {
            "v": 1,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "status": self.status,
        }
        if self.reason is not None:
            body["reason"] = self.reason
        return body


def unit_name(run_id: str) -> str:
    return f"switchboard-night-{run_id}.service"


class Controller:
    """Turns one validated socket request into journal transitions and root-owned unit actions.

    The caller is untrusted even when its uid is a configured agent: every start is re-authorized against a
    fresh server grant, and a launch is only ever attempted after the journal persisted its claim.
    """

    def __init__(
        self,
        config: VerifierConfig,
        journal: LocalJournal,
        grants: GrantSource,
        runs: RunManager,
        clock: Callable[[], ClockReading] = clock_reading,
        boottime: Callable[[], float] = lambda: time.clock_gettime(time.CLOCK_BOOTTIME),
    ) -> None:
        self._config = config
        self._journal = journal
        self._grants = grants
        self._runs = runs
        self._clock = clock
        self._boottime = boottime

    def handle(self, request: Request, peer_uid: int) -> Response:
        allowed = self._config.principals_for_uid(peer_uid)
        if not allowed:
            return self._refuse(request, "peer_not_configured")
        try:
            if request.op == "start":
                return self._start(request, allowed)
            if request.op == "stop":
                return self._stop(request, allowed)
            return self._status(request, allowed)
        except StateError as error:
            return self._refuse(request, str(error))

    def _start(self, request: Request, allowed: frozenset[str]) -> Response:
        before = self._boottime()
        try:
            grant = self._grants.fetch_grant(request.run_id, request.fence)
        except (GrantUnavailable, OSError) as error:
            logger.warning("grant fetch for run %s failed: %s", request.run_id, error)
            return self._refuse(request, "grant_unavailable")
        round_trip = max(0.0, self._boottime() - before)
        why = self._inadmissible(grant, allowed, round_trip)
        if why is not None:
            return self._refuse(request, why)
        agent = self._config.agents[grant.principal]
        now = self._clock()
        remaining = grant.remaining_seconds - round_trip
        reservation = self._journal.reserve(
            Grant(
                request.run_id,
                request.fence,
                agent.principal,
                agent.uid,
                now.boot_id,
                now.boottime + remaining,
                min(now.utc + remaining, grant.window_end_utc),
            )
        )
        record = reservation.record
        if not self._journal.claim_start(request.run_id, request.fence):
            return self._from_record(request, self._current(record))
        runtime = min(record.deadline_boottime - now.boottime, record.deadline_utc - now.utc)
        runtime -= self._config.limits.stop_grace_seconds
        try:
            self._runs.launch(RunSpec(request.run_id, unit_name(request.run_id), agent, runtime))
        except _MANAGER_FAILURES as error:
            logger.error("launch of run %s is uncertain: %s", request.run_id, error)
            self._journal.quarantine(request.run_id, request.fence, "launch_uncertain")
            return Response(request.request_id, request.run_id, "quarantined", "launch_uncertain")
        return Response(request.request_id, request.run_id, "running")

    def _stop(self, request: Request, allowed: frozenset[str]) -> Response:
        record = self._owned(request, allowed)
        if record is None:
            return self._refuse(request, "unknown_run")
        record = self._journal.request_stop(request.run_id, request.fence)
        if not record.launch_claimed:
            return self._from_record(request, record)
        unit = unit_name(request.run_id)
        try:
            self._runs.stop(unit)
            seen = self._runs.inspect(unit)
        except _MANAGER_FAILURES as error:
            logger.error("stopping run %s failed: %s", request.run_id, error)
            self._journal.quarantine(request.run_id, request.fence, "proof_incomplete")
            return Response(request.request_id, request.run_id, "quarantined", "proof_incomplete")
        if seen.unit_active or seen.cgroup_populated is not False:
            return Response(request.request_id, request.run_id, "stopping")
        return Response(request.request_id, request.run_id, "stopped")

    def _status(self, request: Request, allowed: frozenset[str]) -> Response:
        record = self._owned(request, allowed)
        if record is None:
            return self._refuse(request, "unknown_run")
        return self._from_record(request, record)

    def _inadmissible(self, grant: ServerGrant, allowed: frozenset[str], round_trip: float) -> str | None:
        if grant.box != self._config.box:
            return "wrong_box"
        if grant.principal not in allowed:
            return "wrong_principal"
        if grant.state not in ADMISSIBLE_STATES:
            return "not_admissible"
        if grant.remaining_seconds - round_trip - self._config.limits.stop_grace_seconds <= MIN_START_SECONDS:
            return "insufficient_time"
        return None

    def _owned(self, request: Request, allowed: frozenset[str]) -> Record | None:
        for record in self._journal.active():
            if record.run_id == request.run_id:
                if record.principal not in allowed or not secrets.compare_digest(record.fence, request.fence):
                    return None
                return record
        return None

    def _current(self, record: Record) -> Record:
        return next((each for each in self._journal.active() if each.run_id == record.run_id), record)

    def _from_record(self, request: Request, record: Record) -> Response:
        statuses: dict[str, Status] = {
            "reserved": "starting",
            "starting": "running",
            "stopping": "stopping",
            "quarantined": "quarantined",
            "stopped": "stopped",
        }
        return Response(request.request_id, request.run_id, statuses[record.state], record.reason)

    @staticmethod
    def _refuse(request: Request, reason: str) -> Response:
        return Response(request.request_id, request.run_id, "refused", reason)
