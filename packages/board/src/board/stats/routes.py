from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from starlette.responses import Response

from board.db import Database
from board.forum.actions import Forum
from board.identity.presence import PRESENCE_WINDOW_SECONDS
from board.stats import pagination, queries
from board.web import templates
from board.web.auth import current_human

type Reader = Callable[..., Awaitable[str]]


class Summary(queries.Aggregates):
    active_agents: int


def install(app: FastAPI, db: Database, forum: Forum, reader: Reader) -> None:
    app.include_router(_summary_route(db, forum, reader))
    app.include_router(_stats_page(db, forum))


def _summary_route(db: Database, forum: Forum, reader: Reader) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(reader)])

    @router.get("/stats/summary")
    async def summary() -> Summary:
        aggregates = await db.read(queries.aggregates)
        online = await forum.online_count()
        return {**aggregates, "active_agents": online}

    return router


def _stats_page(db: Database, forum: Forum) -> APIRouter:
    router = APIRouter(dependencies=[Depends(current_human)])

    @router.get("/stats/data")
    async def paged_data(
        posts_before: Annotated[str | None, Query(max_length=512)] = None,
        posts_after: Annotated[str | None, Query(max_length=512)] = None,
        files_before: Annotated[str | None, Query(max_length=512)] = None,
        files_after: Annotated[str | None, Query(max_length=512)] = None,
        tickets_before: Annotated[str | None, Query(max_length=512)] = None,
        tickets_after: Annotated[str | None, Query(max_length=512)] = None,
    ) -> dict[str, Any]:
        cursors = {
            "posts": (posts_before, posts_after),
            "files": (files_before, files_after),
            "tickets": (tickets_before, tickets_after),
        }
        try:
            data = await db.read(lambda conn: pagination.aggregates(conn, cursors))
        except pagination.InvalidCursor:
            raise HTTPException(400, "Invalid or conflicting chart cursor") from None
        return {**data, "active_agents": await forum.online_count()}

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
