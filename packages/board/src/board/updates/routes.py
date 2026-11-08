from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from board.updates import NotOffered, Unavailable, Updates
from board.web import templates
from board.web.auth import current_human, same_origin

_REFUSALS = {
    "unavailable": "This board has no updater installed, so it cannot update itself.",
    "not_offered": "That version is no longer the one on offer. Check again and retry.",
}


def install(app: FastAPI, updates: Updates) -> None:
    app.state.updates = updates
    app.include_router(_pages(updates))


def _pages(updates: Updates) -> APIRouter:
    router = APIRouter(prefix="/updates", dependencies=[Depends(same_origin), Depends(current_human)])

    @router.get("")
    async def updates_page(request: Request, error: str = "") -> Response:
        context = {"status": updates.status(), "refusal": _REFUSALS.get(error)}
        return templates.TemplateResponse(request, "updates/index.html", context)

    @router.get("/panel")
    async def panel() -> Response:
        template = templates.get_template("updates/index.html")
        context = template.new_context({"status": updates.status()})
        return HTMLResponse("".join(template.blocks["panel"](context)))

    @router.post("/check")
    async def check() -> Response:
        try:
            updates.request_check()
        except Unavailable:
            return _back("unavailable")
        return _back()

    @router.post("/apply")
    async def apply(commit: Annotated[str, Form()]) -> Response:
        try:
            updates.request_apply(commit)
        except Unavailable:
            return _back("unavailable")
        except NotOffered:
            return _back("not_offered")
        return _back()

    return router


def _back(error: str = "") -> RedirectResponse:
    return RedirectResponse(f"/updates?error={error}" if error else "/updates", status_code=303)
