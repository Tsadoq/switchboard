import mimetypes
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from board.dist import Distribution, NotDistributed, install_script

SHELL_SCRIPT = "text/x-shellscript"
_NO_STORE = {"Cache-Control": "no-store"}


def install(app: FastAPI, distribution: Distribution, public_url: str) -> None:
    app.include_router(_routes(distribution, public_url))
    app.exception_handler(NotDistributed)(_not_distributed)


def _routes(distribution: Distribution, public_url: str) -> APIRouter:
    router = APIRouter()

    @router.get("/install.sh")
    async def install_sh() -> Response:
        script = await run_in_threadpool(install_script, distribution, public_url)
        return Response(script, media_type=SHELL_SCRIPT, headers=_NO_STORE)

    @router.get("/dist/{name:path}")
    async def dist_file(name: str) -> FileResponse:
        path = await run_in_threadpool(distribution.file, name)
        return FileResponse(path, media_type=_media_type(path), headers=_NO_STORE)

    return router


def _media_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


async def _not_distributed(request: Request, missing: NotDistributed) -> JSONResponse:
    return JSONResponse({"reason": "no_such_dist_file"}, status_code=404)
