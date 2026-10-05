"""The HTTP interface of the gateway, for the other services (never for the outside world).

Every route takes and returns the normalized contracts. Errors, authentication and the body limit
are the ones every service shares (``factotum.web``).
"""

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

from factotum.contracts.base import Contract
from factotum.contracts.chat import ChatRequest
from factotum.contracts.embeddings import EmbeddingRequest
from factotum.contracts.models import ModelRole
from factotum.contracts.speech import (
    SynthesizeRequest,
    SynthesizeResult,
    TranscribeRequest,
    TranscribeResult,
)
from factotum.model_gateway.core import ModelGateway
from factotum.web import Authenticator, Caller, create_base_app

MAX_BODY_BYTES = 8 * 1024 * 1024


def _frame(event: Contract) -> str:
    return f"data: {event.model_dump_json()}\n\n"


async def _server_sent[E: Contract](
    first: E | None, rest: AsyncGenerator[E]
) -> AsyncGenerator[str]:
    try:
        if first is not None:
            yield _frame(first)
        async for event in rest:
            yield _frame(event)
    finally:
        # When the client goes away the answer is no longer wanted, and it costs tokens (or is a
        # voice made for nobody).
        await rest.aclose()


def create_app(
    gateway: ModelGateway,
    authenticator: Authenticator,
    *,
    docs: bool = False,
    on_start: Callable[[], Awaitable[None]] | None = None,
    on_stop: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """``on_start`` runs once, before the gateway serves anything: the hub-backed repository of
    bindings uses it to fetch the tenant's bindings before the first request can miss them.
    ``on_stop`` runs after the gateway has closed its own connections, for whatever ``on_start``
    opened (the hub client is not the gateway's to close)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if on_start is not None:
            await on_start()
        try:
            yield
        finally:
            await gateway.aclose()  # the connections it keeps to the providers
            if on_stop is not None:
                await on_stop()

    app = create_base_app(
        "Factotum model gateway",
        authenticator,
        docs=docs,
        max_body_bytes=MAX_BODY_BYTES,
        lifespan=lifespan,
    )

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

    @app.post("/v1/transcribe", response_model=None, summary="Speech-to-text")
    async def transcribe(body: TranscribeRequest, principal: Caller) -> Response:
        text = await gateway.transcribe(principal, body.audio)
        return JSONResponse(TranscribeResult(text=text).model_dump(mode="json"))

    @app.post("/v1/synthesize", response_model=None, summary="Text-to-speech, whole or streamed")
    async def synthesize(body: SynthesizeRequest, principal: Caller) -> Response:
        if not body.stream:
            audio = await gateway.synthesize(principal, body.text)
            return JSONResponse(SynthesizeResult(audio=audio).model_dump(mode="json"))
        stream = gateway.synthesize_stream(principal, body.text)
        # As for a chat: a voice that cannot begin is an HTTP error with its own status, not a 200
        # whose body says the opposite.
        first = await anext(stream, None)
        return StreamingResponse(
            _server_sent(first, stream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/profiles/{role}", response_model=None, summary="What a role can do")
    async def profile(role: ModelRole, principal: Caller) -> Response:
        return JSONResponse((await gateway.profile(principal, role)).model_dump(mode="json"))

    return app
