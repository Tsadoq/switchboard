import time
from functools import partial
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel

from board.constants import HUMAN
from board.db import Database
from board.identity import presence
from board.identity.routes import current_principal
from board.identity.verifier import rfc3339
from board.wakes.bus import Wake, WakeBus

MAX_WAIT_SECONDS = 60

type Inbox = Literal["me", "human"]


class AckRequest(BaseModel):
    ids: list[int]
    principal: Inbox = "me"


def install(app: FastAPI, bus: WakeBus, db: Database) -> None:
    app.include_router(_routes(bus, db))


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
    }
