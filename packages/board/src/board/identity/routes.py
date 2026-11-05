import base64
import binascii

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

from board.identity.tokens import ReadTokens
from board.identity.verifier import Rejected, Verified, Verifier, rfc3339

router = APIRouter(prefix="/api")


class ChallengeRequest(BaseModel):
    principal: str


class SignedRequest(BaseModel):
    envelope: str
    signature: str


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.exception_handler(Rejected)(_rejected)


def encode_envelope(envelope_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(envelope_bytes).decode()


def decode_envelope(text: str) -> bytes:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise Rejected("malformed") from None


async def current_principal(request: Request) -> str:
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    principal = await _tokens(request).authenticate(bearer) if scheme.lower() == "bearer" and bearer else None
    if principal is None:
        raise Rejected("bad_token")
    return principal


@router.post("/auth/challenge")
async def issue_challenge(body: ChallengeRequest, request: Request) -> dict[str, str]:
    if not request.app.state.registry.known(body.principal):
        raise Rejected("unknown_principal")
    challenge = await _tokens(request).issue_challenge(body.principal)
    return {"challenge": challenge.value, "expires_at": rfc3339(challenge.expires_at)}


@router.post("/auth/token")
async def redeem_challenge(body: SignedRequest, request: Request) -> dict[str, str]:
    token = await _tokens(request).redeem(await _verify(request, body))
    return {"token": token.value, "expires_at": rfc3339(token.expires_at)}


@router.post("/signed")
async def signed_action(body: SignedRequest, request: Request) -> Response:
    response: Response = await request.app.state.actions.dispatch(await _verify(request, body))
    return response


async def _verify(request: Request, body: SignedRequest) -> Verified:
    verifier: Verifier = request.app.state.verifier
    return await verifier.verify(decode_envelope(body.envelope), body.signature)


def _tokens(request: Request) -> ReadTokens:
    tokens: ReadTokens = request.app.state.tokens
    return tokens


async def _rejected(request: Request, rejected: Rejected) -> JSONResponse:
    return JSONResponse({"reason": rejected.reason}, status_code=401)
