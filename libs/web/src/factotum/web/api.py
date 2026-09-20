"""The plumbing every service's HTTP interface shares.

An error is always ``{"error": {"code", "message", "details"}}``, where the code is the stable
one of the typed error and the status says, roughly, whose fault it is: the caller's (4xx), or the
provider's and the configuration's (5xx). What a caller sent is never echoed back: it may be the
text of a conversation or a key.
"""

import logging
import math
from collections.abc import Mapping
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from factotum.contracts.errors import (
    AuthError,
    BindingNotFoundError,
    CollectionNotFoundError,
    ConflictError,
    ContentFilteredError,
    ContextTooLongError,
    DocumentNotFoundError,
    DocumentTooLargeError,
    EmbeddingNotConfiguredError,
    FactotumError,
    GatewayError,
    GatewayUnavailableError,
    InvalidRequestError,
    InvalidSchemaError,
    InvalidStructuredOutputError,
    NotFoundError,
    PolicyViolationError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    RoleNotSupportedError,
    SecretNotFoundError,
    UnsupportedDocumentError,
    UrlNotAllowedError,
)
from factotum.contracts.identity import Principal

logger = logging.getLogger("factotum.web")

DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024

_STATUS: dict[type, int] = {
    AuthError: 401,
    PolicyViolationError: 403,
    BindingNotFoundError: 404,
    NotFoundError: 404,
    CollectionNotFoundError: 404,
    DocumentNotFoundError: 404,
    ConflictError: 409,
    EmbeddingNotConfiguredError: 409,
    UnsupportedDocumentError: 415,
    DocumentTooLargeError: 413,
    GatewayError: 502,
    GatewayUnavailableError: 503,
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


def register_status(error: type[FactotumError], status: int) -> None:
    """Say which status an error has, for the errors a service adds."""
    _STATUS[error] = status


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
"""A route parameter of this type is the authenticated principal of the request."""


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


def create_base_app(
    title: str,
    authenticator: Authenticator,
    *,
    docs: bool = False,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> FastAPI:
    """An application with the authenticator, the body limit and the error handlers in place.

    The service adds its routes. ``docs`` serves the interactive documentation, which is for
    development.
    """
    app = FastAPI(
        title=title,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.authenticator = authenticator
    app.add_middleware(BodySizeLimit, max_bytes=max_body_bytes)

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
        logger.exception("unhandled error")
        return JSONResponse(error_body("internal_error", "internal error"), status_code=500)

    return app
