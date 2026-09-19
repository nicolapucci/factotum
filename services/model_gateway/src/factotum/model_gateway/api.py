"""The HTTP interface of the gateway, for the other services (never for the outside world).

Every route takes and returns the normalized contracts. An error is always
``{"error": {"code", "message", "details"}}``, where the code is the stable one of the typed error
and the status says, roughly, whose fault it is: the caller's (4xx), or the provider's and the
configuration's (5xx).
"""

import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from factotum.contracts.chat import ChatRequest, EventUnion
from factotum.contracts.embeddings import EmbeddingRequest
from factotum.contracts.errors import (
    AuthError,
    BindingNotFoundError,
    ContentFilteredError,
    ContextTooLongError,
    FactotumError,
    InvalidRequestError,
    InvalidSchemaError,
    InvalidStructuredOutputError,
    PolicyViolationError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    RoleNotSupportedError,
    SecretNotFoundError,
    UrlNotAllowedError,
)
from factotum.contracts.identity import Principal
from factotum.contracts.models import ModelRole
from factotum.model_gateway.core import ModelGateway

logger = logging.getLogger("factotum.model_gateway")

MAX_BODY_BYTES = 8 * 1024 * 1024

_STATUS: dict[type, int] = {
    AuthError: 401,
    PolicyViolationError: 403,
    BindingNotFoundError: 404,
    ContextTooLongError: 413,
    InvalidSchemaError: 422,
    InvalidRequestError: 422,
    RoleNotSupportedError: 422,
    ContentFilteredError: 422,
    RateLimitedError: 429,
    ProviderTimeoutError: 504,
    ProviderAuthError: 502,
    InvalidStructuredOutputError: 502,
    SecretNotFoundError: 502,
    UrlNotAllowedError: 502,
    ProviderError: 502,
    FactotumError: 500,
}


def status_for(error: FactotumError) -> int:
    """The status of the most specific class registered for the error or one of its parents."""
    return next(_STATUS[kind] for kind in type(error).__mro__ if kind in _STATUS)


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Principal: ...


def error_body(code: str, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": dict(details or {})}}


def _principal_of(request: Request) -> Principal:
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator.authenticate(request.headers)


Caller = Annotated[Principal, Depends(_principal_of)]


class BodySizeLimit:
    """Refuses a request that declares a body bigger than ``max_bytes``, before reading it."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            declared = dict(scope["headers"]).get(b"content-length", b"")
            if declared.isdigit() and int(declared) > self._max_bytes:
                refusal = JSONResponse(
                    error_body("payload_too_large", "the request body is too large"),
                    status_code=413,
                )
                await refusal(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _frame(event: EventUnion) -> str:
    return f"data: {event.model_dump_json()}\n\n"


async def _server_sent(
    first: EventUnion | None, rest: AsyncGenerator[EventUnion]
) -> AsyncIterator[str]:
    try:
        if first is not None:
            yield _frame(first)
        async for event in rest:
            yield _frame(event)
    finally:
        # When the client goes away the answer is no longer wanted, and it costs tokens.
        await rest.aclose()


def create_app(
    gateway: ModelGateway, authenticator: Authenticator, *, docs: bool = False
) -> FastAPI:
    app = FastAPI(
        title="Factotum model gateway",
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )

    app.state.authenticator = authenticator
    app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)

    @app.exception_handler(FactotumError)
    async def typed_error(request: Request, error: FactotumError) -> JSONResponse:
        headers: dict[str, str] = {}
        if isinstance(error, RateLimitedError) and error.retry_after_s is not None:
            headers["Retry-After"] = str(math.ceil(error.retry_after_s))
        return JSONResponse(
            jsonable_encoder(error_body(error.code, error.message, error.details)),
            status_code=status_for(error),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Where and why, never the value: it may be the text of a conversation or a key.
        problems = [
            {"loc": list(problem["loc"]), "msg": problem["msg"], "type": problem["type"]}
            for problem in error.errors()
        ]
        return JSONResponse(
            jsonable_encoder(
                error_body("invalid_request_body", "the request is not valid", {"errors": problems})
            ),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        logger.exception("unhandled error in the model gateway")
        return JSONResponse(error_body("internal_error", "internal error"), status_code=500)

    @app.get("/healthz", summary="Liveness")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", summary="Readiness")
    async def readyz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat", response_model=None, summary="Chat, whole or streamed")
    async def chat(body: ChatRequest, principal: Caller) -> Response:
        if not body.stream:
            result = await gateway.chat(principal, body)
            return JSONResponse(result.model_dump(mode="json"))
        stream = gateway.chat_stream(principal, body)
        # Waiting for the first event lets a failure to start be an HTTP error with its own status,
        # instead of a 200 whose body says the opposite.
        first = await anext(stream, None)
        return StreamingResponse(
            _server_sent(first, stream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/embeddings", response_model=None, summary="Embeddings")
    async def embeddings(body: EmbeddingRequest, principal: Caller) -> Response:
        result = await gateway.embed(principal, body)
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/v1/profiles/{role}", response_model=None, summary="What a role can do")
    async def profile(role: ModelRole, principal: Caller) -> Response:
        return JSONResponse((await gateway.profile(principal, role)).model_dump(mode="json"))

    return app
