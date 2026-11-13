import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from board.asks.actions import LIST_MAX, Ask, AskRefused, Asks
from board.identity.verifier import Verified, rfc3339

type Reader = Callable[..., Awaitable[str]]
type SignedAction = Callable[[Verified], Awaitable[Response]]

_ID = re.compile(r"[0-9]{1,18}")


def install(app: FastAPI, asks: Asks, reader: Reader) -> None:
    app.include_router(_read_routes(asks, reader))
    app.exception_handler(AskRefused)(_refused)
    for action, handler in _signed_actions(asks).items():
        app.state.actions.register(action, handler)


def _read_routes(asks: Asks, reader: Reader) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(reader)])

    @router.get("/asks")
    async def list_asks(
        state: Literal["open", "closed"] = "open", limit: int = Query(LIST_MAX, ge=1, le=LIST_MAX)
    ) -> list[dict[str, Any]]:
        return [ask_json(ask) for ask in await asks.listing(state, limit)]

    @router.get("/asks/{ask_id}")
    async def read_ask(ask_id: int) -> dict[str, Any]:
        return ask_json(await asks.get(ask_id))

    return router


def _signed_actions(asks: Asks) -> dict[str, SignedAction]:
    async def open_ask(verified: Verified) -> Response:
        envelope = verified.envelope
        ask = await asks.open(
            envelope.principal, _target_id(envelope.target, "post"), envelope.payload.get("ask")
        )
        return JSONResponse(ask_json(ask), status_code=201)

    async def close_ask(verified: Verified) -> Response:
        envelope = verified.envelope
        ask = await asks.close(
            envelope.principal, _target_id(envelope.target, "ask"), envelope.payload.get("outcome")
        )
        return JSONResponse(ask_json(ask))

    return {"ask.open": open_ask, "ask.close": close_ask}


def _target_id(target: str, expected: str) -> int:
    scope, _, key = target.partition(":")
    if scope != expected or _ID.fullmatch(key) is None:
        raise AskRefused("bad_target")
    return int(key)


async def _refused(request: Request, refused: AskRefused) -> JSONResponse:
    body = (
        {"reason": refused.reason}
        if refused.field is None
        else {"reason": refused.reason, "field": refused.field}
    )
    return JSONResponse(body, status_code=refused.status)


def ask_json(ask: Ask) -> dict[str, Any]:
    return {
        "id": ask.id,
        "post_id": ask.post_id,
        "thread_id": ask.thread_id,
        "thread_title": ask.thread_title,
        "asker": ask.asker,
        "ask": ask.ask,
        "created_at": rfc3339(ask.created_at),
        "closed_at": None if ask.closed_at is None else rfc3339(ask.closed_at),
        "closed_by": ask.closed_by,
        "outcome": ask.outcome,
    }
