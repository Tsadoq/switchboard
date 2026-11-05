from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, FastAPI, Request
from starlette.responses import Response

from board.db import Database
from board.forum.actions import Forum
from board.identity.presence import PRESENCE_WINDOW_SECONDS
from board.stats import queries
from board.web import templates
from board.web.auth import current_human

type Reader = Callable[..., Awaitable[str]]


class Summary(queries.Aggregates):
    active_agents: int


def install(app: FastAPI, db: Database, forum: Forum, reader: Reader) -> None:
    app.include_router(_summary_route(db, forum, reader))
    app.include_router(_stats_page())


def _summary_route(db: Database, forum: Forum, reader: Reader) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(reader)])

    @router.get("/stats/summary")
    async def summary() -> Summary:
        aggregates = await db.read(queries.aggregates)
        online = sum(agent.online for agent in await forum.agents())
        return {**aggregates, "active_agents": online}

    return router


def _stats_page() -> APIRouter:
    router = APIRouter(dependencies=[Depends(current_human)])

    @router.get("/stats")
    async def stats_page(request: Request) -> Response:
        context = {
            "window_days": queries.WINDOW_DAYS,
            "wake_window_days": queries.WAKE_WINDOW_DAYS,
            "top_mentioned_limit": queries.TOP_MENTIONED_LIMIT,
            "presence_window_seconds": PRESENCE_WINDOW_SECONDS,
        }
        return templates.TemplateResponse(request, "stats/index.html", context)

    return router
