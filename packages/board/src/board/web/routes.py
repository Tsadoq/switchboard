import hashlib
from collections.abc import AsyncIterator, Awaitable
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, File, Form, Path, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from board.constants import HUMAN
from board.files import Declaration, FileEntry, Files
from board.forum.actions import Forum, Refused
from board.forum.entities import Post, SubforumTarget, Thread, ThreadTarget
from board.tickets import COLUMNS
from board.tickets.actions import Conflict, NoSuchTicket, Tickets
from board.web import STATIC_DIR, templates
from board.web.auth import (
    CrossSite,
    LoginRequired,
    LoginThrottled,
    current_human,
    human_sessions,
    same_origin,
)

STOP_POLLING = 286
_CLOSED_COLUMN = "done"
_READ_CHUNK_BYTES = 64 * 1024
_FALLBACK_CONTENT_TYPE = "application/octet-stream"
_TICKET_ERRORS = (Conflict, NoSuchTicket, Refused)
_TICKET_REFUSALS = {
    "already_claimed": "Someone else already claimed that ticket. Release it from their side first.",
    "no_such_ticket": "That ticket no longer exists.",
    "bad_payload": "The board refused that change. Reload the board and try again.",
    "post_too_large": "That description is over the board's post size limit.",
}


def install(app: FastAPI, forum: Forum, tickets: Tickets, files: Files) -> None:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(_login_pages())
    app.include_router(_forum_pages(forum, tickets, files))
    app.include_router(_ticket_pages(tickets))
    app.include_router(_file_pages(files))
    app.include_router(_agent_pages(forum))
    app.exception_handler(LoginRequired)(_to_login)
    app.exception_handler(CrossSite)(_cross_site)


def _login_pages() -> APIRouter:
    router = APIRouter(dependencies=[Depends(same_origin)])

    @router.get("/login")
    async def login_page(request: Request) -> Response:
        return _page(request, "login.html", failed=False, throttled=False)

    @router.post("/login")
    async def log_in(request: Request, password: Annotated[str, Form()] = "") -> Response:
        response = _see_other("/")
        address = request.client.host if request.client else ""
        try:
            logged_in = await human_sessions(request).log_in(address, password, response)
        except LoginThrottled as throttled:
            page = _page(request, "login.html", status_code=429, failed=False, throttled=True)
            page.headers["Retry-After"] = str(throttled.retry_after)
            return page
        if logged_in:
            return response
        return _page(request, "login.html", status_code=401, failed=True, throttled=False)

    @router.post("/logout")
    async def log_out(request: Request) -> Response:
        response = _see_other("/login")
        await human_sessions(request).log_out(request, response)
        return response

    return router


def _forum_pages(forum: Forum, tickets: Tickets, files: Files) -> APIRouter:
    router = APIRouter(dependencies=[Depends(same_origin), Depends(current_human)])

    @router.get("/")
    async def forum_page(request: Request) -> Response:
        return _page(request, "forum/index.html", subforums=await forum.activity())

    @router.get("/mentions")
    async def mentions_page(
        request: Request,
        before: Annotated[int, Query(ge=0)] = 0,
        archived: bool = False,
    ) -> Response:
        through, unread_count = await forum.unread_mention_snapshot(HUMAN)
        found = await forum.mentioned_posts(HUMAN, before, archived=archived)
        return _page(
            request,
            "forum/mentions.html",
            mentions=found[:50],
            archived=archived,
            through=through,
            unread_count=unread_count,
            next_before=found[49][1].id if len(found) > 50 else None,
        )

    @router.post("/mentions/read-all")
    async def read_all_mentions(through: Annotated[int, Form(ge=0, le=2**63 - 1)]) -> Response:
        await forum.read_all_mentions(HUMAN, through)
        return _see_other("/mentions")

    @router.post("/mentions/{post_id}/read")
    async def read_mention(post_id: int) -> Response:
        await forum.set_mention_read(HUMAN, post_id, read=True)
        return _see_other("/mentions")

    @router.post("/mentions/{post_id}/unread")
    async def unread_mention(post_id: int) -> Response:
        await forum.set_mention_read(HUMAN, post_id, read=False)
        return _see_other("/mentions?archived=true")

    @router.get("/mention-options")
    async def mention_options(q: Annotated[str, Query(max_length=100)] = "") -> Response:
        agents = await forum.agents()
        names = {agent.principal for agent in agents}
        names.update(principal.partition("@")[0] for principal in tuple(names))
        names.update({"human", "all"})
        matches = sorted(name for name in names if name.casefold().startswith(q.casefold()))
        return JSONResponse([{"value": f"@{name}"} for name in matches[:20]])

    @router.get("/f/{slug}")
    async def subforum_page(
        request: Request,
        slug: str,
        before: Annotated[str | None, Query(max_length=512)] = None,
        after: Annotated[str | None, Query(max_length=512)] = None,
    ) -> Response:
        try:
            page = await forum.subforum_page(slug, before=before, after=after)
        except Refused as refusal:
            if refusal.reason != "bad_payload":
                raise
            return _page(
                request,
                "forum/subforum.html",
                subforum=await forum.subforum(slug),
                threads=(),
                page=None,
                older=None,
                newer=None,
                error="Invalid thread page cursor. Return to Latest to start again.",
                status_code=400,
            )
        return _page(
            request,
            "forum/subforum.html",
            subforum=page.subforum,
            threads=page.threads,
            page=page,
            error=None,
            older=f"/f/{slug}?" + urlencode({"before": page.older}) if page.older is not None else None,
            newer=f"/f/{slug}?" + urlencode({"after": page.newer}) if page.newer is not None else None,
        )

    @router.post("/f/{slug}/archive")
    async def archive_room(slug: str) -> Response:
        await forum.set_room_archived(HUMAN, slug, archived=True)
        return _see_other(f"/f/{slug}")

    @router.post("/f/{slug}/unarchive")
    async def unarchive_room(slug: str) -> Response:
        await forum.set_room_archived(HUMAN, slug, archived=False)
        return _see_other(f"/f/{slug}")

    @router.post("/f/{slug}/threads")
    async def open_thread(slug: str, title: Annotated[str, Form()], body: Annotated[str, Form()]) -> Response:
        opened = await forum.post(HUMAN, "human", SubforumTarget(slug), {"title": title, "body": body})
        return _see_other(f"/t/{opened.thread_id}")

    @router.get("/p/{post_id}")
    async def post_permalink(post_id: Annotated[int, Path(ge=1, le=2**63 - 1)]) -> Response:
        thread_id, on_latest_page = await forum.post_context(post_id)
        cursor = "" if on_latest_page else f"?before={post_id + 1}"
        return _see_other(f"/t/{thread_id}{cursor}#post-{post_id}")

    @router.get("/t/{thread_id}")
    async def thread_page(
        request: Request,
        thread_id: int,
        before: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
        after: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
    ) -> Response:
        page = await forum.thread_page(thread_id, before=before, after=after)
        thread, posts = page.thread, list(page.posts)
        closed = await _is_closed(tickets, thread)
        attached = await _attached(files, posts)
        room_archived = any(
            room.slug == thread.subforum and room.archived for room in await forum.subforums()
        )
        return _page(
            request,
            "forum/thread.html",
            thread=thread,
            posts=posts,
            closed=closed,
            attached=attached,
            room_archived=room_archived,
            page=page,
            live=bool(posts) and page.newer is None and not closed,
        )

    @router.get("/t/{thread_id}/posts")
    async def thread_posts(
        thread_id: int,
        after: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
        before: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
    ) -> Response:
        page = await forum.thread_page(thread_id, before=before, after=after)
        thread, posts = page.thread, list(page.posts)
        closed = await _is_closed(tickets, thread)
        status_code = STOP_POLLING if closed and page.newer is None else 200
        attached = await _attached(files, posts)
        response = _fragment(
            "forum/thread.html",
            "posts",
            status_code,
            thread=thread,
            posts=posts,
            closed=closed,
            attached=attached,
        )
        response.headers["X-Next-After"] = str(posts[-1].id if posts else (after or 0))
        response.headers["X-Has-More"] = "true" if page.newer is not None else "false"
        response.headers["X-Post-Total"] = str(page.total)
        return response

    @router.post("/t/{thread_id}/reply")
    async def reply(thread_id: int, body: Annotated[str, Form()]) -> Response:
        posted = await forum.post(HUMAN, "human", ThreadTarget(thread_id), {"body": body})
        return _see_other(f"/p/{posted.post_id}")

    return router


def _ticket_pages(tickets: Tickets) -> APIRouter:
    router = APIRouter(prefix="/tickets", dependencies=[Depends(same_origin), Depends(current_human)])

    @router.get("")
    async def board_page(request: Request, error: str = "") -> Response:
        board = await tickets.previews()
        return _page(request, "tickets/board.html", board=board, refusal=_TICKET_REFUSALS.get(error))

    @router.get("/columns")
    async def board_columns() -> Response:
        return _fragment("tickets/board.html", "columns", board=await tickets.previews())

    @router.get("/list")
    async def ticket_list(
        request: Request,
        column: Annotated[str, Query(max_length=20)] = "",
        assignee: Annotated[str, Query(max_length=200)] = "",
        unassigned: bool = False,
        before: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
        after: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
    ) -> Response:
        if unassigned:
            assignee = ""
        try:
            page = await tickets.page(
                column=column or None,
                assignee=assignee or None,
                unassigned=unassigned,
                before=before,
                after=after,
            )
        except Refused:
            return _page(
                request,
                "tickets/list.html",
                status_code=400,
                page=None,
                older=None,
                newer=None,
                columns=COLUMNS,
                column=column,
                assignee=assignee,
                unassigned=unassigned,
                error="Choose a valid column and use only one page cursor. Return to Latest to start again.",
                latest="/tickets/list",
            )
        filters = {key: value for key, value in {"column": column, "assignee": assignee}.items() if value}
        if unassigned:
            filters["unassigned"] = "true"

        def link(**cursor: int) -> str:
            query = urlencode(filters | cursor)
            return "/tickets/list" + (f"?{query}" if query else "")

        return _page(
            request,
            "tickets/list.html",
            page=page,
            columns=COLUMNS,
            column=column,
            assignee=assignee,
            unassigned=unassigned,
            latest=link(),
            error=None,
            older=link(before=page.older) if page.older is not None else None,
            newer=link(after=page.newer) if page.newer is not None else None,
        )

    @router.post("")
    async def create(
        title: Annotated[str, Form()],
        body: Annotated[str, Form()],
        column: Annotated[str, Form()] = COLUMNS[0],
    ) -> Response:
        return await _back_to_board(
            tickets.create(HUMAN, "human", {"title": title, "body": body, "column": column})
        )

    @router.post("/{ticket_id}/claim")
    async def claim(ticket_id: int) -> Response:
        return await _back_to_board(tickets.claim(HUMAN, ticket_id))

    @router.post("/{ticket_id}/release")
    async def release(ticket_id: int) -> Response:
        return await _back_to_board(tickets.release(HUMAN, ticket_id))

    @router.post("/{ticket_id}/move")
    async def move(ticket_id: int, column: Annotated[str, Form()]) -> Response:
        return await _back_to_board(tickets.move(HUMAN, ticket_id, column))

    return router


def _file_pages(files: Files) -> APIRouter:
    router = APIRouter(prefix="/files", dependencies=[Depends(same_origin), Depends(current_human)])

    @router.get("")
    async def files_page(
        request: Request,
        before: Annotated[str | None, Query(max_length=512)] = None,
        after: Annotated[str | None, Query(max_length=512)] = None,
    ) -> Response:
        try:
            page = await files.page(before=before, after=after)
        except Refused:
            return _page(
                request,
                "files/index.html",
                page=None,
                files=(),
                older=None,
                newer=None,
                error="Invalid file page cursor. Return to Latest to start again.",
                status_code=400,
            )
        return _page(
            request,
            "files/index.html",
            page=page,
            files=page.files,
            error=None,
            older="/files?" + urlencode({"before": page.older}) if page.older is not None else None,
            newer="/files?" + urlencode({"after": page.newer}) if page.newer is not None else None,
        )

    @router.post("")
    async def upload(
        blob: Annotated[UploadFile, File()], ttl_days: Annotated[int | None, Form()] = None
    ) -> Response:
        await _put_upload(files, blob, ttl_days)
        return _see_other("/files")

    return router


def _agent_pages(forum: Forum) -> APIRouter:
    router = APIRouter(dependencies=[Depends(same_origin), Depends(current_human)])

    @router.get("/agents")
    async def agents_page(request: Request, mention: str = "") -> Response:
        agents = await forum.agents()
        if mention:
            agents = [
                agent
                for agent in agents
                if agent.principal == mention or agent.principal.partition("@")[0] == mention
            ]
        return _page(request, "agents/index.html", agents=agents)

    return router


def _page(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _fragment(name: str, block: str, status_code: int = 200, **context: Any) -> Response:
    template = templates.get_template(name)
    html = "".join(template.blocks[block](template.new_context(context)))
    return HTMLResponse(html, status_code=status_code)


async def _back_to_board(action: Awaitable[object]) -> Response:
    try:
        await action
    except _TICKET_ERRORS as refused:
        return _see_other(f"/tickets?error={_ticket_reason(refused)}")
    return _see_other("/tickets")


def _see_other(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def _to_login(request: Request, required: LoginRequired) -> Response:
    if request.headers.get("hx-request") == "true":
        # The XHR behind an htmx poll follows a 303 by itself and htmx would swap the login page into
        # the polled element; HX-Redirect on a non-redirect status makes htmx navigate the whole page.
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return _see_other("/login")


async def _cross_site(request: Request, refused: CrossSite) -> Response:
    return JSONResponse({"reason": "cross_site"}, status_code=403)


async def _is_closed(tickets: Tickets, thread: Thread) -> bool:
    if thread.kind != "ticket":
        return False
    ticket = await tickets.for_thread(thread.id)
    return ticket is not None and ticket.column == _CLOSED_COLUMN


def _ticket_reason(refused: Exception) -> str:
    if isinstance(refused, Conflict):
        return "already_claimed"
    if isinstance(refused, NoSuchTicket):
        return "no_such_ticket"
    assert isinstance(refused, Refused)
    return refused.reason


async def _attached(files: Files, posts: list[Post]) -> dict[str, FileEntry]:
    ids = tuple(dict.fromkeys(file_id for post in posts for file_id in post.files))
    if not ids:
        return {}
    return {entry.id: entry for entry in await files.live_by_ids(ids)}


async def _put_upload(files: Files, upload: UploadFile, ttl_days: int | None) -> None:
    digest = hashlib.sha256()
    size = 0
    async for chunk in _chunks(upload):
        digest.update(chunk)
        size += len(chunk)
    media_type = (upload.content_type or "").partition(";")[0].strip().lower()
    declared = Declaration.from_payload(
        {
            "name": upload.filename or "",
            "content_type": media_type or _FALLBACK_CONTENT_TYPE,
            "size": size,
            "sha256": digest.hexdigest(),
            "ttl_days": ttl_days,
        }
    )
    await upload.seek(0)
    await files.put(_chunks(upload), declared, HUMAN)


async def _chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(_READ_CHUNK_BYTES):
        yield chunk
