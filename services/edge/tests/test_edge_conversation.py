import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEVICE_HEADER, SUBJECT_HEADER, DevPrincipalDelegation
from factotum.contracts.conversation import (
    Delta,
    Failed,
    Finished,
    Phase,
    ReferenceKind,
    Started,
    Status,
    TurnEvent,
)
from factotum.contracts.errors import (
    AuthError,
    ChatNotConfiguredError,
    ContextTooLongError,
    ConversationNotFoundError,
    ConversationUnavailableError,
)
from factotum.contracts.ids import ConversationId, DeviceId, ExchangeId
from factotum.contracts.testing import make_tenant_id, make_tenant_principal, make_user_principal
from factotum.edge.conversation import ConversationClient

TENANT = make_tenant_id()
KITCHEN = DeviceId(uuid4())
SHARED = make_tenant_principal(tenant_id=TENANT, device_id=KITCHEN)
FINISHED = Finished(
    reference=ReferenceKind.NONE, exchanges_used=0, passages_used=0, untrusted_content=False
)


def frame(event: TurnEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def stream_of(*events: TurnEvent) -> bytes:
    return "".join(frame(event) for event in events).encode()


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> ConversationClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://conversation")
    return ConversationClient(
        http,
        DevPrincipalDelegation(ServiceSettings(_env_file=None, environment=Environment.TEST)),
    )


async def events_of(client: ConversationClient, message: str = "hello") -> list[TurnEvent]:
    return [event async for event in client.turn(SHARED, message)]


def answering(*events: TurnEvent) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream_of(*events))

    return handler


def error_body(code: str, message: str = "no") -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": {}}}


THE_ANSWER: list[TurnEvent] = [
    Started(conversation_id=ConversationId(uuid4()), exchange_id=ExchangeId(uuid4())),
    Status(phase=Phase.SEARCHING),
    Delta(text="Hold the button "),
    Delta(text="for ten seconds."),
    FINISHED,
]


class TestWhatIsAsked:
    async def test_the_message_is_asked_as_a_turn_for_whom_the_device_acts_for(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=stream_of(*THE_ANSWER))

        await events_of(client_for(handler), "how do I reset the router?")

        [request] = seen
        assert (request.method, request.url.path) == ("POST", "/v1/turns")
        assert json.loads(request.content) == {"message": "how do I reset the router?"}
        assert request.headers[SUBJECT_HEADER] == "tenant"
        assert request.headers[DEVICE_HEADER] == str(KITCHEN)

    async def test_a_person_is_asked_for_as_a_person(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=stream_of(*THE_ANSWER))

        personal = make_user_principal(tenant_id=TENANT, device_id=KITCHEN)
        client = client_for(handler)

        _ = [event async for event in client.turn(personal, "hello")]

        assert seen[0].headers[SUBJECT_HEADER] == "user"

    def test_it_acts_in_development_and_in_tests_only(self) -> None:
        with pytest.raises(AuthError, match="disabled outside"):
            DevPrincipalDelegation(
                ServiceSettings(_env_file=None, environment=Environment.PRODUCTION)
            )


class TestWhatIsHeard:
    async def test_the_events_of_the_turn_come_in_the_order_they_were_sent(self) -> None:
        events = await events_of(client_for(answering(*THE_ANSWER)))

        assert events == THE_ANSWER

    async def test_a_frame_that_arrives_in_pieces_is_still_one_frame(self) -> None:
        payload = stream_of(*THE_ANSWER)

        class Slowly(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                for start in range(0, len(payload), 7):
                    yield payload[start : start + 7]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=Slowly())

        assert await events_of(client_for(handler)) == THE_ANSWER

    async def test_what_is_not_a_frame_of_data_is_let_go(self) -> None:
        text = ": a comment\n\n" + stream_of(*THE_ANSWER).decode() + "\n\nevent: ping\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=text.encode())

        assert await events_of(client_for(handler)) == THE_ANSWER

    async def test_an_answer_that_failed_is_an_answer_that_ended(self) -> None:
        failure = Failed(code="gateway_unavailable", message="the model is away")

        events = await events_of(client_for(answering(THE_ANSWER[0], failure)))

        assert events[-1] == failure


class TestWhatCannotBeAnswered:
    @pytest.mark.parametrize(
        ("status", "code", "error"),
        [
            (409, "chat_not_configured", ChatNotConfiguredError),
            (413, "context_too_long", ContextTooLongError),
            (404, "conversation_not_found", ConversationNotFoundError),
        ],
    )
    async def test_an_error_before_the_answer_is_the_error_it_was(
        self, status: int, code: str, error: type[Exception]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=error_body(code, "the reason"))

        with pytest.raises(error, match="the reason"):
            await events_of(client_for(handler))

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(502, text="<html>Bad gateway</html>"),
            httpx.Response(503, json={"nothing": "here"}),
            httpx.Response(500, json=error_body("no_such_error")),
            httpx.Response(200, text=""),
            httpx.Response(200, text="data: not json\n\n"),
            httpx.Response(200, text='data: {"type": "dance"}\n\n'),
        ],
    )
    async def test_anything_else_is_a_conversation_service_that_is_not_answering(
        self, response: httpx.Response
    ) -> None:
        with pytest.raises(ConversationUnavailableError):
            await events_of(client_for(lambda request: response))

    async def test_what_is_not_reached_is_not_answering(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(ConversationUnavailableError, match="could not be reached"):
            await events_of(client_for(handler))

    async def test_an_answer_cut_off_is_not_an_answer_that_ended(self) -> None:
        class Cut(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield stream_of(*THE_ANSWER[:3])
                raise httpx.ReadError("the connection dropped")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=Cut())

        seen: list[TurnEvent] = []
        failure: Exception | None = None
        try:
            async for event in client_for(handler).turn(SHARED, "hello"):
                seen.append(event)  # noqa: PERF401 - what came before the failure is what is looked at
        except ConversationUnavailableError as error:
            failure = error

        assert failure is not None
        assert seen == THE_ANSWER[:3]  # what came before is not taken back

    async def test_an_answer_that_stops_without_saying_it_is_over_is_refused(self) -> None:
        with pytest.raises(ConversationUnavailableError, match="before it was finished"):
            await events_of(client_for(answering(*THE_ANSWER[:3])))


class TestBeingReady:
    @pytest.mark.parametrize(("status", "ready"), [(200, True), (503, False), (500, False)])
    async def test_the_service_is_ready_when_it_says_so(self, status: int, ready: bool) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/readyz"
            return httpx.Response(status, json={"status": "x"})

        assert await client_for(handler).ready() is ready

    async def test_a_service_that_is_not_there_is_not_ready(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        assert await client_for(handler).ready() is False
