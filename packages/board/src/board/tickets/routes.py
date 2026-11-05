import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from board.forum.actions import Refused
from board.identity.verifier import Verified, rfc3339
from board.tickets.actions import Conflict, NoSuchTicket, Ticket, Tickets

type Reader = Callable[..., Awaitable[str]]
type SignedAction = Callable[[Verified], Awaitable[Response]]

_TICKET_ID = re.compile(r"[0-9]{1,18}")


def install(app: FastAPI, tickets: Tickets, reader: Reader) -> None:
    app.include_router(_read_routes(tickets, reader))
    app.exception_handler(Conflict)(_conflict)
    app.exception_handler(NoSuchTicket)(_no_such_ticket)
    for action, handler in _signed_actions(tickets).items():
        app.state.actions.register(action, handler)


def _read_routes(tickets: Tickets, reader: Reader) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(reader)])

    @router.get("/tickets")
    async def list_tickets(column: str | None = None, assignee: str | None = None) -> list[dict[str, Any]]:
        return [_ticket_json(ticket) for ticket in await tickets.find(column, assignee)]

    @router.get("/tickets/{ticket_id}")
    async def read_ticket(ticket_id: int) -> dict[str, Any]:
        return _ticket_json(await tickets.get(ticket_id))

    return router


def _signed_actions(tickets: Tickets) -> dict[str, SignedAction]:
    async def create(verified: Verified) -> Response:
        envelope = verified.envelope
        ticket = await tickets.create(envelope.principal, "agent", envelope.payload, signed=verified)
        return JSONResponse(_ticket_json(ticket), status_code=201)

    async def claim(verified: Verified) -> Response:
        envelope = verified.envelope
        ticket = await tickets.claim(envelope.principal, _ticket_id(envelope.target))
        return JSONResponse(_ticket_json(ticket))

    async def release(verified: Verified) -> Response:
        envelope = verified.envelope
        ticket = await tickets.release(envelope.principal, _ticket_id(envelope.target))
        return JSONResponse(_ticket_json(ticket))

    async def move(verified: Verified) -> Response:
        envelope = verified.envelope
        column = envelope.payload.get("column", "")
        ticket = await tickets.move(envelope.principal, _ticket_id(envelope.target), column)
        return JSONResponse(_ticket_json(ticket))

    return {"ticket.create": create, "ticket.claim": claim, "ticket.release": release, "ticket.move": move}


def _ticket_id(target: str) -> int:
    scope, _, key = target.partition(":")
    if scope != "ticket" or _TICKET_ID.fullmatch(key) is None:
        raise Refused("bad_target")
    return int(key)


async def _conflict(request: Request, conflict: Conflict) -> JSONResponse:
    return JSONResponse({"reason": "already_claimed", "assignee": conflict.assignee}, status_code=409)


async def _no_such_ticket(request: Request, missing: NoSuchTicket) -> JSONResponse:
    return JSONResponse({"reason": "no_such_ticket"}, status_code=404)


def _ticket_json(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "title": ticket.title,
        "column": ticket.column,
        "assignee": ticket.assignee,
        "thread_id": ticket.thread_id,
        "created_by": ticket.created_by,
        "created_at": rfc3339(ticket.created_at),
        "updated_at": rfc3339(ticket.updated_at),
        "done_at": None if ticket.done_at is None else rfc3339(ticket.done_at),
    }
