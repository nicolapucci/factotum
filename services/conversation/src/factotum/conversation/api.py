"""The HTTP interface of the conversation service.

Everything is under ``/v1`` and needs an authenticated caller. A turn (``POST /v1/turns``) answers
as server-sent events, one frame for each event of the turn, so that a speaker can begin to speak
before the answer is complete. What makes an answer impossible (no chat model, a message the model
cannot take, a conversation that is not the caller's) is an HTTP error with its own status, like the
other services; what fails once the answer has begun is its last event, ``failed``.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Lifespan

from factotum.contracts.conversation import (
    ConversationDetail,
    ConversationList,
    HistoryQuery,
    HistoryResult,
    TurnEvent,
    TurnRequest,
)
from factotum.contracts.ids import ConversationId
from factotum.conversation.service import ConversationService
from factotum.web import Authenticator, Caller, create_base_app

MAX_BODY_BYTES = 64 * 1024


def _service(request: Request) -> ConversationService:
    service: ConversationService = request.app.state.service
    return service


Service = Annotated[ConversationService, Depends(_service)]


def _frame(event: TurnEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


async def _server_sent(first: TurnEvent, rest: AsyncGenerator[TurnEvent]) -> AsyncGenerator[str]:
    try:
        yield _frame(first)
        async for event in rest:
            yield _frame(event)
    finally:
        # When the caller goes away the answer is no longer wanted, and it costs tokens.
        await rest.aclose()


def create_app(
    authenticator: Authenticator,
    *,
    service: ConversationService | None = None,
    docs: bool = False,
    lifespan: Lifespan[FastAPI] | None = None,
    ready: Callable[[], Awaitable[bool]] | None = None,
) -> FastAPI:
    """The service as a web application. It is given the operations to serve (or, when it is put
    together while it starts, ``lifespan`` sets ``app.state.service``) and, for ``/readyz``, a way
    to tell whether what it depends on answers."""
    app = create_base_app(
        "Factotum conversation",
        authenticator,
        docs=docs,
        max_body_bytes=MAX_BODY_BYTES,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.ready = ready

    @app.get("/healthz", summary="Liveness")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", summary="Readiness", response_model=None)
    async def readyz(request: Request) -> Response:
        check: Callable[[], Awaitable[bool]] | None = request.app.state.ready
        answering = True if check is None else await check()
        return JSONResponse(
            {"status": "ok" if answering else "unavailable"}, status_code=200 if answering else 503
        )

    @app.post("/v1/turns", response_model=None, summary="A message and its answer, streamed")
    async def turn(body: TurnRequest, principal: Caller, service: Service) -> Response:
        stream = service.turn(principal, body)
        # The first event is always `started`: waiting for it lets a failure to begin be an HTTP
        # error with its own status, instead of a 200 whose body says the opposite.
        first = await anext(stream)
        return StreamingResponse(
            _server_sent(first, stream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/conversations", summary="The caller's conversations, the last active first")
    async def list_conversations(
        principal: Caller,
        service: Service,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ConversationList:
        return await service.list_conversations(principal, limit=limit, offset=offset)

    @app.get("/v1/conversations/{conversation_id}", summary="A conversation and what was said")
    async def get_conversation(
        conversation_id: ConversationId, principal: Caller, service: Service
    ) -> ConversationDetail:
        return await service.get_conversation(principal, conversation_id)

    @app.delete(
        "/v1/conversations/{conversation_id}",
        status_code=204,
        response_class=Response,
        summary="Forget a conversation",
    )
    async def delete_conversation(
        conversation_id: ConversationId, principal: Caller, service: Service
    ) -> Response:
        await service.delete_conversation(principal, conversation_id)
        return Response(status_code=204)

    @app.delete("/v1/history", summary="Forget everything the caller said")
    async def forget_everything(principal: Caller, service: Service) -> dict[str, int]:
        return {"conversations": await service.forget_everything(principal)}

    @app.post("/v1/history/search", summary="Look for something that was said before")
    async def search_history(
        body: HistoryQuery, principal: Caller, service: Service
    ) -> HistoryResult:
        return await service.search_history(principal, body)

    return app
