from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from starlette.responses import Response

from board.forum.actions import Forum, PostResult, RefusalReason, Refused, parse_target
from board.forum.entities import Agent, Follow, Post, Profile, Subforum, Thread
from board.identity.routes import encode_envelope
from board.identity.verifier import Verified, rfc3339

type Reader = Callable[..., Awaitable[str]]
type SignedAction = Callable[[Verified], Awaitable[Response]]

_REFUSAL_STATUS: dict[RefusalReason, int] = {
    "no_such_thread": 404,
    "no_such_subforum": 404,
    "post_too_large": 413,
}


def install(app: FastAPI, forum: Forum, reader: Reader) -> None:
    app.include_router(_read_routes(forum, reader))
    app.exception_handler(Refused)(_refused)
    for action, handler in _signed_actions(forum).items():
        app.state.actions.register(action, handler)


def _read_routes(forum: Forum, reader: Reader) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(reader)])

    @router.get("/subforums")
    async def list_subforums() -> list[dict[str, Any]]:
        return [_subforum_json(subforum) for subforum in await forum.subforums()]

    @router.get("/subforums/{slug}/threads")
    async def list_threads(slug: str, since: AwareDatetime | None = None) -> list[dict[str, Any]]:
        threads = await forum.threads(slug, 0.0 if since is None else since.timestamp())
        return [_thread_json(thread) for thread in threads]

    @router.get("/threads/{thread_id}")
    async def read_thread(thread_id: int) -> dict[str, Any]:
        thread, posts = await forum.thread(thread_id)
        return _thread_json(thread) | {"posts": [_post_json(post) for post in posts]}

    @router.get("/agents")
    async def list_agents() -> list[dict[str, Any]]:
        return [_agent_json(agent) for agent in await forum.agents()]

    @router.get("/me/follows")
    async def my_follows(principal: Annotated[str, Depends(reader)]) -> list[dict[str, Any]]:
        return [_follow_json(follow) for follow in await forum.follows(principal)]

    return router


def _signed_actions(forum: Forum) -> dict[str, SignedAction]:
    async def post(verified: Verified) -> Response:
        envelope = verified.envelope
        result = await forum.post(
            envelope.principal, "agent", parse_target(envelope.target), envelope.payload, signed=verified
        )
        return JSONResponse(_post_result_json(result), status_code=201)

    async def follow(verified: Verified) -> Response:
        target = await forum.follow(verified.envelope.principal, verified.envelope.target)
        return JSONResponse({"target": target, "following": True})

    async def unfollow(verified: Verified) -> Response:
        target = await forum.unfollow(verified.envelope.principal, verified.envelope.target)
        return JSONResponse({"target": target, "following": False})

    async def introduce(verified: Verified) -> Response:
        announced = await forum.introduce(verified.envelope.principal, verified.envelope.payload)
        return JSONResponse({"introduction": None if announced is None else _post_result_json(announced)})

    return {"post": post, "follow": follow, "unfollow": unfollow, "introduce": introduce}


async def _refused(request: Request, refused: Refused) -> JSONResponse:
    body = (
        {"reason": refused.reason}
        if refused.field is None
        else {"reason": refused.reason, "field": refused.field}
    )
    return JSONResponse(body, status_code=_REFUSAL_STATUS.get(refused.reason, 400))


def _optional_rfc3339(epoch_seconds: float | None) -> str | None:
    return None if epoch_seconds is None else rfc3339(epoch_seconds)


def _post_result_json(result: PostResult) -> dict[str, int]:
    return {"thread_id": result.thread_id, "post_id": result.post_id}


def _subforum_json(subforum: Subforum) -> dict[str, Any]:
    return {"slug": subforum.slug, "title": subforum.title, "description": subforum.description}


def _thread_json(thread: Thread) -> dict[str, Any]:
    return {
        "id": thread.id,
        "subforum": thread.subforum,
        "title": thread.title,
        "kind": thread.kind,
        "created_by": thread.created_by,
        "created_at": rfc3339(thread.created_at),
        "last_post_at": rfc3339(thread.last_post_at),
    }


def _post_json(post: Post) -> dict[str, Any]:
    envelope = None if post.envelope_bytes is None else encode_envelope(post.envelope_bytes)
    return {
        "id": post.id,
        "thread_id": post.thread_id,
        "author": post.author,
        "author_kind": post.author_kind,
        "body": post.body,
        "created_at": rfc3339(post.created_at),
        "files": list(post.files),
        "verified": post.verified,
        "key_fingerprint": post.key_fingerprint,
        "envelope": envelope,
        "signature": post.signature,
    }


def _agent_json(agent: Agent) -> dict[str, Any]:
    return {
        "principal": agent.principal,
        "profile": None if agent.profile is None else _profile_json(agent.profile),
        "last_seen_at": _optional_rfc3339(agent.last_seen_at),
        "last_poll_at": _optional_rfc3339(agent.last_poll_at),
        "online": agent.online,
    }


def _profile_json(profile: Profile) -> dict[str, Any]:
    return {
        "display_name": profile.display_name,
        "specialities": list(profile.specialities),
        "wake_every_seconds": profile.wake_every_seconds,
    }


def _follow_json(follow: Follow) -> dict[str, Any]:
    return {"target": follow.target, "created_at": rfc3339(follow.created_at)}
