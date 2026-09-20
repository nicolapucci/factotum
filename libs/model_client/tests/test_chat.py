import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import respx

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.chat import (
    ChatMessage,
    ChatRequest,
    ErrorEvent,
    EventUnion,
    FinishEvent,
    FinishReason,
    MessageRole,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.errors import (
    ChatNotConfiguredError,
    GatewayError,
    GatewayUnavailableError,
    ProviderUnavailableError,
)
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.models import ModelRole
from factotum.model_client import DevDelegation, GatewayChat

URL = "http://gateway.test"
USER = Owner(kind=OwnerKind.USER, id=uuid4())
TENANT = Owner(kind=OwnerKind.TENANT, id=uuid4())
ASK = ChatRequest(messages=(ChatMessage(role=MessageRole.USER, content="hello"),))


def frames(*events: EventUnion) -> bytes:
    return b"".join(f"data: {event.model_dump_json()}\n\n".encode() for event in events)


def error(code: str, message: str = "it failed") -> dict[str, object]:
    return {"error": {"code": code, "message": message, "details": {}}}


@pytest.fixture
async def chat() -> AsyncIterator[GatewayChat]:
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)
    async with httpx.AsyncClient(base_url=URL) as client:
        yield GatewayChat(client, DevDelegation(settings))


async def read(stream: AsyncIterator[EventUnion]) -> list[EventUnion]:
    return [event async for event in stream]


class TestAWholeAnswer:
    @respx.mock
    async def test_the_request_goes_to_the_gateway_for_the_owner_and_the_answer_comes_back(
        self, chat: GatewayChat
    ) -> None:
        route = respx.post(f"{URL}/v1/chat").respond(
            200, json={"text": "hi", "finish_reason": "stop", "usage": {"input_tokens": 3}}
        )

        result = await chat.chat(TENANT, ASK)

        assert (result.text, result.finish_reason) == ("hi", FinishReason.STOP)
        assert result.usage == Usage(input_tokens=3)
        sent = route.calls.last.request
        assert sent.headers["x-factotum-dev-subject"] == "tenant"
        body = json.loads(sent.content)
        assert (body["role"], body["stream"]) == ("chat.main", False)

    @respx.mock
    async def test_a_structured_answer_comes_back_parsed(self, chat: GatewayChat) -> None:
        respx.post(f"{URL}/v1/chat").respond(
            200, json={"text": "{}", "parsed": {"a": 1}, "finish_reason": "stop"}
        )
        request = ASK.model_copy(update={"response_schema": {"type": "object"}})

        result = await chat.chat(USER, request)

        assert result.parsed == {"a": 1}

    @respx.mock
    async def test_a_request_to_stream_is_still_answered_whole(self, chat: GatewayChat) -> None:
        route = respx.post(f"{URL}/v1/chat").respond(
            200, json={"text": "hi", "finish_reason": "stop"}
        )

        await chat.chat(USER, ASK.model_copy(update={"stream": True}))

        assert json.loads(route.calls.last.request.content)["stream"] is False

    @respx.mock
    async def test_the_kind_of_an_error_survives_the_trip(self, chat: GatewayChat) -> None:
        respx.post(f"{URL}/v1/chat").respond(503, json=error("provider_unavailable"))

        with pytest.raises(ProviderUnavailableError, match="it failed"):
            await chat.chat(USER, ASK)

    @respx.mock
    async def test_no_model_for_the_role_is_no_chat_model_and_not_no_embedding_model(
        self, chat: GatewayChat
    ) -> None:
        respx.post(f"{URL}/v1/chat").respond(404, json=error("binding_not_found"))

        with pytest.raises(ChatNotConfiguredError):
            await chat.chat(USER, ASK)

    @respx.mock
    async def test_a_gateway_that_cannot_be_reached_is_unavailable(self, chat: GatewayChat) -> None:
        respx.post(f"{URL}/v1/chat").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(GatewayUnavailableError):
            await chat.chat(USER, ASK)

    @respx.mock
    async def test_an_answer_that_is_not_a_chat_answer_is_refused(self, chat: GatewayChat) -> None:
        respx.post(f"{URL}/v1/chat").respond(200, json={"nothing": "useful"})

        with pytest.raises(GatewayError, match="not a chat answer"):
            await chat.chat(USER, ASK)


class TestAStreamedAnswer:
    @respx.mock
    async def test_the_events_come_in_the_order_they_were_sent(self, chat: GatewayChat) -> None:
        route = respx.post(f"{URL}/v1/chat").respond(
            200,
            content=frames(
                TextDelta(text="Hel"),
                TextDelta(text="lo"),
                UsageEvent(usage=Usage(output_tokens=2)),
                FinishEvent(reason=FinishReason.STOP),
            ),
            headers={"content-type": "text/event-stream"},
        )

        events = await read(chat.chat_stream(USER, ASK))

        assert [event.type for event in events] == ["text_delta", "text_delta", "usage", "finish"]
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello"
        assert json.loads(route.calls.last.request.content)["stream"] is True

    @respx.mock
    async def test_what_is_not_a_data_line_is_ignored(self, chat: GatewayChat) -> None:
        body = b": a comment\n\nevent: ping\n\n" + frames(TextDelta(text="hi"))
        respx.post(f"{URL}/v1/chat").respond(200, content=body)

        events = await read(chat.chat_stream(USER, ASK))

        assert events == [TextDelta(text="hi")]

    @respx.mock
    async def test_a_failure_after_the_start_arrives_as_an_event(self, chat: GatewayChat) -> None:
        respx.post(f"{URL}/v1/chat").respond(
            200,
            content=frames(
                TextDelta(text="hi"), ErrorEvent(code="provider_timeout", message="slow")
            ),
        )

        events = await read(chat.chat_stream(USER, ASK))

        assert events[-1] == ErrorEvent(code="provider_timeout", message="slow")

    @respx.mock
    async def test_a_failure_before_the_start_is_raised_when_the_stream_is_read(
        self, chat: GatewayChat
    ) -> None:
        respx.post(f"{URL}/v1/chat").respond(404, json=error("binding_not_found"))
        stream = chat.chat_stream(USER, ASK)  # nothing has happened yet

        with pytest.raises(ChatNotConfiguredError):
            await anext(stream)

    @respx.mock
    async def test_a_gateway_that_cannot_be_reached_is_unavailable(self, chat: GatewayChat) -> None:
        respx.post(f"{URL}/v1/chat").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(GatewayUnavailableError):
            await read(chat.chat_stream(USER, ASK))

    @respx.mock
    async def test_an_event_that_is_not_one_is_refused(self, chat: GatewayChat) -> None:
        respx.post(f"{URL}/v1/chat").respond(200, content=b'data: {"type": "surprise"}\n\n')

        with pytest.raises(GatewayError, match="not a chat event"):
            await read(chat.chat_stream(USER, ASK))


class TestProfile:
    @respx.mock
    async def test_what_a_role_can_do_for_the_owner(self, chat: GatewayChat) -> None:
        route = respx.get(f"{URL}/v1/profiles/chat.fast").respond(
            200,
            json={
                "role": "chat.fast",
                "max_input_tokens": 4096,
                "max_output_tokens": 512,
                "streaming": True,
                "structured_output": "native",
            },
        )

        profile = await chat.profile(TENANT, ModelRole.CHAT_FAST)

        assert (profile.max_input_tokens, profile.max_output_tokens) == (4096, 512)
        assert route.calls.last.request.headers["x-factotum-dev-subject"] == "tenant"

    @respx.mock
    async def test_a_role_with_no_model_is_no_chat_model(self, chat: GatewayChat) -> None:
        respx.get(f"{URL}/v1/profiles/chat.main").respond(404, json=error("binding_not_found"))

        with pytest.raises(ChatNotConfiguredError):
            await chat.profile(USER, ModelRole.CHAT_MAIN)

    @respx.mock
    async def test_a_gateway_that_cannot_be_reached_is_unavailable(self, chat: GatewayChat) -> None:
        respx.get(f"{URL}/v1/profiles/chat.main").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(GatewayUnavailableError):
            await chat.profile(USER, ModelRole.CHAT_MAIN)

    @respx.mock
    async def test_an_answer_that_is_not_a_profile_is_refused(self, chat: GatewayChat) -> None:
        respx.get(f"{URL}/v1/profiles/chat.main").respond(200, json={"nothing": "useful"})

        with pytest.raises(GatewayError, match="not a profile"):
            await chat.profile(USER, ModelRole.CHAT_MAIN)
