import json
import time
from functools import partial
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from pydantic import BaseModel, Field

from board.constants import HUMAN
from board.db import Database
from board.identity import presence
from board.identity.routes import current_principal
from board.identity.verifier import rfc3339
from board.wakes.bus import Wake, WakeBus

MAX_WAIT_SECONDS = 60
MAX_RUNS = 16
MAX_RUN_THREADS = 20

type Inbox = Literal["me", "human"]


class AckRequest(BaseModel):
    ids: list[int]
    principal: Inbox = "me"


class Run(BaseModel):
    threads: list[Annotated[int, Field(ge=1, le=2**63 - 1)]] = Field(max_length=MAX_RUN_THREADS)
    since: float = Field(ge=0, allow_inf_nan=False)


class ActivityReport(BaseModel):
    running: list[Run] = Field(max_length=MAX_RUNS)
    ttl_seconds: int = Field(ge=10, le=600)


def install(app: FastAPI, bus: WakeBus, db: Database) -> None:
    app.include_router(_routes(bus, db))
    app.include_router(_activity_route(db))


def _activity_route(db: Database) -> APIRouter:
    router = APIRouter()

    @router.post("/api/activity", status_code=204)
    async def report(body: ActivityReport, caller: Annotated[str, Depends(current_principal)]) -> Response:
        now = time.time()
        running = json.dumps([{"threads": run.threads, "since": min(run.since, now)} for run in body.running])
        await db.transact(
            partial(presence.record_running, principal=caller, running=running, until=now + body.ttl_seconds)
        )
        return Response(status_code=204)

    return router


def _routes(bus: WakeBus, db: Database) -> APIRouter:
    router = APIRouter(prefix="/api/wakes")

    @router.get("")
    async def poll(
        request: Request,
        caller: Annotated[str, Depends(current_principal)],
        wait: Annotated[float, Query(ge=0, le=MAX_WAIT_SECONDS)] = 0,
        principal: Inbox = "me",
    ) -> list[dict[str, Any]]:
        await db.transact(partial(presence.record_poll, principal=caller, at=time.time()))
        wakes = await bus.wait_then_lease(_owner(caller, principal), wait, request.is_disconnected)
        return [_wake_json(wake) for wake in wakes]

    @router.post("/ack")
    async def ack(body: AckRequest, caller: Annotated[str, Depends(current_principal)]) -> dict[str, int]:
        return {"acked": await bus.ack(_owner(caller, body.principal), body.ids)}

    return router


def _owner(caller: str, inbox: Inbox) -> str:
    return HUMAN if inbox == "human" else caller


def _wake_json(wake: Wake) -> dict[str, Any]:
    return {
        "id": wake.id,
        "principal": wake.principal,
        "reason": wake.reason,
        "source_ref": wake.source_ref,
        "summary": wake.summary,
        "created_at": rfc3339(wake.created_at),
        "leased_until": rfc3339(wake.leased_until),
        "thread_id": wake.thread_id,
        "topic_id": wake.topic_id,
    }
