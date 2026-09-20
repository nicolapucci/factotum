"""The HTTP interface of the gateway, for the other services (never for the outside world).

Every route takes and returns the normalized contracts. Errors, authentication and the body limit
are the ones every service shares (``factotum.web``).
"""

from collections.abc import AsyncGenerator

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

from factotum.contracts.chat import ChatRequest, EventUnion
from factotum.contracts.embeddings import EmbeddingRequest
from factotum.contracts.models import ModelRole
from factotum.model_gateway.core import ModelGateway
from factotum.web import Authenticator, Caller, create_base_app

MAX_BODY_BYTES = 8 * 1024 * 1024


def _frame(event: EventUnion) -> str:
    return f"data: {event.model_dump_json()}\n\n"


async def _server_sent(
    first: EventUnion | None, rest: AsyncGenerator[EventUnion]
) -> AsyncGenerator[str]:
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
    app = create_base_app(
        "Factotum model gateway", authenticator, docs=docs, max_body_bytes=MAX_BODY_BYTES
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

    @app.get("/v1/profiles/{role}", response_model=None, summary="What a role can do")
    async def profile(role: ModelRole, principal: Caller) -> Response:
        return JSONResponse((await gateway.profile(principal, role)).model_dump(mode="json"))

    return app
