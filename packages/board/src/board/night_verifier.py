import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ConfigDict

from board.db import Database
from board.identity.roles import VerifierRoles
from board.identity.routes import SignedRequest, _verify
from board.identity.verifier import Verified
from board.night import AdmissionDenied, Run, _find, settle_in_transaction, start_in_transaction, window_for


class Denied(ValueError):
    pass


class VerifierRequest(SignedRequest):
    model_config = ConfigDict(extra="forbid")


class NightVerifier:
    def __init__(self, db: Database, roles: VerifierRoles, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._roles = roles
        self._clock = clock

    async def dispatch(self, verified: Verified) -> dict[str, Any]:
        box = self._roles.authorize(verified)
        envelope = verified.envelope
        handlers = {
            "night.state": self._state,
            "night.bootstrap": self._bootstrap,
            "night.grant": self._grant,
            "night.binding": self._binding,
            "night.evidence": self._evidence,
        }
        handler = handlers.get(envelope.action)
        if handler is None:
            raise Denied("unsupported_action")
        return await self._db.transact(lambda conn: handler(conn, box, envelope.target, envelope.payload))

    def _state(
        self, conn: sqlite3.Connection, box: str, target: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _fields(payload, set())
        if target != "self":
            raise Denied("invalid_target")
        row = conn.execute("SELECT * FROM night_verifier_state WHERE box = ?", (box,)).fetchone()
        active = conn.execute(
            "SELECT r.id, r.principal, r.fence, r.state, r.deadline, b.boot_id, b.invocation_id, b.cgroup "
            "FROM night_runs r LEFT JOIN night_verifier_bindings b ON b.run_id = r.id "
            "WHERE r.box = ? AND r.state != 'finished'",
            (box,),
        ).fetchall()
        return {
            "box": box,
            "journal_id": row["journal_id"] if row else None,
            "counter": row["counter"] if row else 0,
            "active": [dict(run) for run in active],
        }

    def _bootstrap(
        self, conn: sqlite3.Connection, box: str, target: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _fields(payload, {"journal_id", "receipt_id"})
        _uuid(payload["journal_id"])
        if target != "self":
            raise Denied("invalid_target")
        cached = self._cached(conn, box, target, payload, "night.bootstrap")
        if cached is not None:
            return self._state(conn, box, "self", {})
        row = conn.execute("SELECT * FROM night_verifier_state WHERE box = ?", (box,)).fetchone()
        if row is not None:
            if row["journal_id"] != payload["journal_id"]:
                raise Denied("journal_mismatch")
        else:
            if conn.execute(
                "SELECT 1 FROM night_runs WHERE box = ? AND state != 'finished' LIMIT 1", (box,)
            ).fetchone():
                raise Denied("unreconciled_runs")
            conn.execute(
                "INSERT INTO night_verifier_state (box, journal_id) VALUES (?, ?)",
                (box, payload["journal_id"]),
            )
        response = self._state(conn, box, "self", {})
        return self._save(conn, box, target, payload, "night.bootstrap", response)

    def _grant(
        self, conn: sqlite3.Connection, box: str, target: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _fields(payload, {"fence"})
        run = self._run(conn, box, target, payload["fence"])
        now = self._clock()
        budget = conn.execute(
            "SELECT ends_at FROM night_budget WHERE principal = ? AND local_date = ?",
            (run.principal, run.local_date),
        ).fetchone()
        if budget is None:
            raise Denied("missing_budget")
        state = run.state
        enrolled = conn.execute(
            "SELECT enabled FROM night_enrollment WHERE principal = ?", (run.principal,)
        ).fetchone()
        enabled = conn.execute("SELECT enabled FROM night_control WHERE singleton = 1").fetchone()[0]
        night = window_for(now)
        if self._roles.is_service(run.principal):
            state = "excluded"
        elif not enabled:
            state = "disabled"
        elif enrolled is None or not enrolled[0]:
            state = "not_enrolled"
        elif (
            night is None
            or night.local_date != run.local_date
            or now < run.admitted_at
            or now >= run.deadline
        ):
            state = "expired"
        return {
            "principal": run.principal,
            "box": run.box,
            "state": state,
            "remaining_seconds": max(0.0, min(run.reserved, run.deadline - now)),
            "window_end_utc": budget["ends_at"],
        }

    def _binding(
        self, conn: sqlite3.Connection, box: str, target: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _fields(
            payload, {"fence", "journal_id", "counter", "receipt_id", "boot_id", "invocation_id", "cgroup"}
        )
        run = self._run(conn, box, target, payload["fence"])
        self._roles.require_agent(run.principal)
        _binding_fields(target, payload)
        cached = self._cached(conn, box, target, payload, "night.binding")
        if cached is not None:
            return cached
        state = self._journal(conn, box, payload["journal_id"])
        if type(payload["counter"]) is not int or payload["counter"] != state["counter"]:
            raise Denied("counter_mismatch")
        previous = conn.execute(
            "SELECT * FROM night_verifier_bindings WHERE run_id = ?", (target,)
        ).fetchone()
        if previous is not None:
            raise Denied("binding_already_recorded")
        now = self._clock()
        start_in_transaction(conn, run.principal, run.id, run.fence, now)
        counter = state["counter"] + 1
        conn.execute("UPDATE night_verifier_state SET counter = ? WHERE box = ?", (counter, box))
        conn.execute(
            "INSERT INTO night_verifier_bindings (run_id, journal_id, counter, boot_id, invocation_id, cgroup, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                target,
                payload["journal_id"],
                counter,
                payload["boot_id"],
                payload["invocation_id"],
                payload["cgroup"],
                now,
            ),
        )
        return self._save(
            conn,
            box,
            target,
            payload,
            "night.binding",
            {"journal_id": payload["journal_id"], "counter": counter, "state": "running"},
        )

    def _evidence(
        self, conn: sqlite3.Connection, box: str, target: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _fields(
            payload,
            {
                "fence",
                "journal_id",
                "counter",
                "receipt_id",
                "boot_id",
                "invocation_id",
                "cgroup",
                "outcome",
                "runtime_seconds",
                "reason",
            },
        )
        run = self._run(conn, box, target, payload["fence"])
        _binding_fields(target, payload)
        if payload["outcome"] not in ("empty", "uncertain") or payload["reason"] not in (
            "completed",
            "deadline",
            "off",
            "preempted",
            "recovery",
            "proof_incomplete",
            "guard_lost",
        ):
            raise Denied("invalid_evidence")
        runtime = payload["runtime_seconds"]
        if runtime is not None:
            try:
                valid = type(runtime) in (int, float) and math.isfinite(runtime) and runtime >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise Denied("invalid_runtime")
        cached = self._cached(conn, box, target, payload, "night.evidence")
        if cached is not None:
            return cached
        state = self._journal(conn, box, payload["journal_id"])
        if type(payload["counter"]) is not int or payload["counter"] != state["counter"]:
            raise Denied("counter_mismatch")
        binding = conn.execute("SELECT * FROM night_verifier_bindings WHERE run_id = ?", (target,)).fetchone()
        if binding is None or any(
            binding[key] != payload[key] for key in ("journal_id", "boot_id", "invocation_id", "cgroup")
        ):
            raise Denied("missing_or_conflicting_binding")
        result = settle_in_transaction(
            conn,
            run.principal,
            run.id,
            run.fence,
            elapsed=run.reserved if runtime is None else runtime,
            stopped=payload["outcome"] == "empty",
        )
        counter = state["counter"] + 1
        conn.execute("UPDATE night_verifier_state SET counter = ? WHERE box = ?", (counter, box))
        return self._save(
            conn,
            box,
            target,
            payload,
            "night.evidence",
            {"journal_id": payload["journal_id"], "counter": counter, "state": result.state},
        )

    @staticmethod
    def _run(conn: sqlite3.Connection, box: str, run_id: str, fence: str) -> Run:
        _uuid(run_id)
        if not isinstance(fence, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", fence) is None:
            raise Denied("stale_fence")
        run = _find(conn, run_id)
        if run.box != box or not secrets.compare_digest(run.fence, fence):
            raise Denied("stale_fence")
        return run

    @staticmethod
    def _journal(conn: sqlite3.Connection, box: str, journal_id: str) -> sqlite3.Row:
        _uuid(journal_id)
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM night_verifier_state WHERE box = ?", (box,)
        ).fetchone()
        if row is None or row["journal_id"] != journal_id:
            raise Denied("journal_mismatch")
        return row

    def _cached(
        self, conn: sqlite3.Connection, box: str, target: str, payload: dict[str, Any], action: str
    ) -> dict[str, Any] | None:
        _uuid(payload["receipt_id"])
        row = conn.execute(
            "SELECT * FROM night_verifier_receipts WHERE receipt_id = ?", (payload["receipt_id"],)
        ).fetchone()
        if row is None:
            return None
        if (
            row["box"] != box
            or row["journal_id"] != payload["journal_id"]
            or row["digest"] != _digest(action, target, payload)
        ):
            raise Denied("receipt_conflict")
        response = json.loads(row["response"])
        if not isinstance(response, dict):
            raise Denied("invalid_stored_receipt")
        return response

    def _save(
        self,
        conn: sqlite3.Connection,
        box: str,
        target: str,
        payload: dict[str, Any],
        action: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        conn.execute(
            "INSERT INTO night_verifier_receipts (receipt_id, box, journal_id, digest, response, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                payload["receipt_id"],
                box,
                payload["journal_id"],
                _digest(action, target, payload),
                json.dumps(response),
                self._clock(),
            ),
        )
        return response


def _fields(payload: dict[str, Any], expected: set[str]) -> None:
    if set(payload) != expected:
        raise Denied("invalid_fields")


def _uuid(value: str) -> None:
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise Denied("invalid_uuid")


def _binding_fields(run_id: str, payload: dict[str, Any]) -> None:
    _uuid(payload["journal_id"])
    _uuid(payload["boot_id"])
    if (
        not isinstance(payload["invocation_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", payload["invocation_id"]) is None
        or payload["invocation_id"] == "0" * 32
    ):
        raise Denied("invalid_invocation")
    if payload["cgroup"] != f"/switchboard-night.slice/switchboard-night-{run_id}.service":
        raise Denied("invalid_cgroup")


def _digest(action: str, target: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps([action, target, payload], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def install(app: FastAPI, authority: NightVerifier) -> None:
    router = APIRouter()

    @router.post("/api/night/verifier")
    async def dispatch(body: VerifierRequest, request: Request) -> JSONResponse:
        verified = await _verify(request, body)
        try:
            response = await authority.dispatch(verified)
        except (Denied, AdmissionDenied) as error:
            return JSONResponse({"reason": str(error)}, status_code=409)
        return JSONResponse(response)

    app.include_router(router)
