"""What every adapter must do the same, whatever its provider does differently.

Each provider is given as the wire format it really speaks. The same normalized request goes to
all of them and the same normalized answers, events and errors must come back: this is what lets
the rest of the system ignore which provider is behind a role.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from factotum.contracts.chat import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    FinishEvent,
    FinishReason,
    MessageRole,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.errors import (
    InvalidRequestError,
    ProviderAuthError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitedError,
    RoleNotSupportedError,
)
from factotum.contracts.models import ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id
from factotum.model_gateway.providers.anthropic import AnthropicAdapter
from factotum.model_gateway.providers.base import ProviderAdapter, ResolvedBinding
from factotum.model_gateway.providers.gemini import GeminiAdapter
from factotum.model_gateway.providers.ollama import OllamaAdapter
from factotum.model_gateway.providers.openai import OpenAIAdapter
from factotum.model_gateway.structured import ChatCall, run_structured
from factotum.model_gateway.testing import (
    FakeServer,
    events,
    json_response,
    mock_client,
    ndjson_response,
    resolved,
    sse_response,
)

KEY = "sk-contract-0123456789abcdefghijklmnopqrstuv"

Reply = Callable[[str, int, int], httpx.Response]
Failure = Callable[[], httpx.Response]


@dataclass(frozen=True)
class Flavor:
    """A provider: its adapter, and the wire format it would answer in."""

    adapter: ProviderAdapter
    binding: ResolvedBinding
    reply: Reply
    cut_off: Callable[[str], httpx.Response]
    stream: Callable[[Sequence[str], int, int], httpx.Response]
    auth_failure: Failure
    rate_limited: Failure
    server_error: Failure
    unknown_model: Failure
    vectors: Callable[[Sequence[Sequence[float]]], httpx.Response] | None
    max_tokens_at: tuple[str, ...]
    temperature_at: tuple[str, ...]

    def call(self, server: FakeServer) -> ChatCall:
        async def chat(request: ChatRequest) -> ChatResult:
            return await self.adapter.chat(server.client, self.binding, request)

        return chat


def bound(
    provider: ProviderKind, *, base_url: str, model: str, key: str | None = KEY
) -> ResolvedBinding:
    binding = make_model_binding(
        tenant_id=make_tenant_id(),
        provider=provider,
        model=model,
        base_url=base_url
        if provider in {ProviderKind.OLLAMA, ProviderKind.OPENAI_COMPATIBLE}
        else None,
        secret_ref="env:KEY" if key else None,
    )
    return resolved(binding, base_url=base_url, api_key=key)


# --- OpenAI and the servers that speak like it ---


def openai_reply(text: str, tokens_in: int, tokens_out: int) -> httpx.Response:
    return json_response(
        {
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
        }
    )


def openai_stream(pieces: Sequence[str], tokens_in: int, tokens_out: int) -> httpx.Response:
    messages: list[tuple[str | None, Any]] = [
        (None, {"choices": [{"delta": {"content": piece}}]}) for piece in pieces
    ]
    messages.append((None, {"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    messages.append(
        (
            None,
            {"choices": [], "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}},
        )
    )
    messages.append((None, "[DONE]"))
    return sse_response(*messages)


def openai_error(status: int, message: str, code: str, **headers: str) -> httpx.Response:
    return json_response(
        {"error": {"message": message, "code": code}}, status=status, headers=headers
    )


def openai_flavor(kind: ProviderKind) -> Flavor:
    official = kind is ProviderKind.OPENAI
    return Flavor(
        adapter=OpenAIAdapter(kind),
        binding=bound(
            kind,
            base_url="https://api.openai.com/v1" if official else "http://localhost:1234/v1",
            model="gpt-4o-mini",
            key=KEY if official else None,
        ),
        reply=openai_reply,
        cut_off=lambda text: json_response(
            {"choices": [{"message": {"content": text}, "finish_reason": "length"}]}
        ),
        stream=openai_stream,
        auth_failure=lambda: openai_error(401, "Incorrect API key", "invalid_api_key"),
        rate_limited=lambda: openai_error(
            429, "Rate limit reached", "rate_limit_exceeded", **{"retry-after": "2"}
        ),
        server_error=lambda: openai_error(500, "The server had an error", "server_error"),
        unknown_model=lambda: openai_error(404, "The model does not exist", "model_not_found"),
        vectors=lambda rows: json_response(
            {"data": [{"index": i, "embedding": list(row)} for i, row in enumerate(rows)]}
        ),
        max_tokens_at=("max_completion_tokens",) if official else ("max_tokens",),
        temperature_at=("temperature",),
    )


# --- Ollama ---


def ollama_reply(text: str, tokens_in: int, tokens_out: int) -> httpx.Response:
    return json_response(
        {
            "message": {"role": "assistant", "content": text},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": tokens_in,
            "eval_count": tokens_out,
        }
    )


def ollama_stream(pieces: Sequence[str], tokens_in: int, tokens_out: int) -> httpx.Response:
    lines: list[dict[str, Any]] = [
        {"message": {"role": "assistant", "content": piece}, "done": False} for piece in pieces
    ]
    lines.append(
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": tokens_in,
            "eval_count": tokens_out,
        }
    )
    return ndjson_response(*lines)


def ollama_flavor() -> Flavor:
    return Flavor(
        adapter=OllamaAdapter(),
        binding=bound(
            ProviderKind.OLLAMA, base_url="http://localhost:11434", model="llama3.1:8b", key=None
        ),
        reply=ollama_reply,
        cut_off=lambda text: json_response(
            {"message": {"content": text}, "done": True, "done_reason": "length"}
        ),
        stream=ollama_stream,
        auth_failure=lambda: json_response({"error": "unauthorized"}, status=401),
        rate_limited=lambda: json_response(
            {"error": "too many requests"}, status=429, headers={"retry-after": "2"}
        ),
        server_error=lambda: json_response(
            {"error": "llama runner process has terminated"}, status=500
        ),
        unknown_model=lambda: json_response({"error": "model 'x' not found"}, status=404),
        vectors=lambda rows: json_response({"embeddings": [list(row) for row in rows]}),
        max_tokens_at=("options", "num_predict"),
        temperature_at=("options", "temperature"),
    )


# --- Anthropic ---


def anthropic_reply(text: str, tokens_in: int, tokens_out: int) -> httpx.Response:
    return json_response(
        {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        }
    )


def anthropic_stream(pieces: Sequence[str], tokens_in: int, tokens_out: int) -> httpx.Response:
    start = {
        "type": "message_start",
        "message": {"usage": {"input_tokens": tokens_in, "output_tokens": 1}},
    }
    messages: list[tuple[str | None, Any]] = [("message_start", start)]
    messages.extend(
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": piece},
            },
        )
        for piece in pieces
    )
    messages.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": tokens_out},
            },
        )
    )
    messages.append(("message_stop", {"type": "message_stop"}))
    return sse_response(*messages)


def anthropic_error(status: int, kind: str, message: str, **headers: str) -> httpx.Response:
    body = {"type": "error", "error": {"type": kind, "message": message}}
    return json_response(body, status=status, headers=headers)


def anthropic_flavor() -> Flavor:
    return Flavor(
        adapter=AnthropicAdapter(),
        binding=bound(
            ProviderKind.ANTHROPIC, base_url="https://api.anthropic.com", model="claude-sonnet-4-5"
        ),
        reply=anthropic_reply,
        cut_off=lambda text: json_response(
            {"content": [{"type": "text", "text": text}], "stop_reason": "max_tokens"}
        ),
        stream=anthropic_stream,
        auth_failure=lambda: anthropic_error(401, "authentication_error", "invalid x-api-key"),
        rate_limited=lambda: anthropic_error(
            429, "rate_limit_error", "Rate limited", **{"retry-after": "2"}
        ),
        server_error=lambda: anthropic_error(529, "overloaded_error", "Overloaded"),
        unknown_model=lambda: anthropic_error(404, "not_found_error", "model: claude-nope"),
        vectors=None,
        max_tokens_at=("max_tokens",),
        temperature_at=("temperature",),
    )


# --- Gemini ---


def gemini_reply(text: str, tokens_in: int, tokens_out: int) -> httpx.Response:
    return json_response(
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": tokens_in, "candidatesTokenCount": tokens_out},
        }
    )


def gemini_stream(pieces: Sequence[str], tokens_in: int, tokens_out: int) -> httpx.Response:
    messages: list[tuple[str | None, Any]] = [
        (None, {"candidates": [{"content": {"role": "model", "parts": [{"text": piece}]}}]})
        for piece in pieces
    ]
    messages.append(
        (
            None,
            {
                "candidates": [{"finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": tokens_in,
                    "candidatesTokenCount": tokens_out,
                },
            },
        )
    )
    return sse_response(*messages)


def gemini_error(status: int, name: str, message: str, **extra: Any) -> httpx.Response:
    return json_response(
        {"error": {"code": status, "message": message, "status": name, **extra}}, status=status
    )


def gemini_flavor() -> Flavor:
    return Flavor(
        adapter=GeminiAdapter(),
        binding=bound(
            ProviderKind.GEMINI,
            base_url="https://generativelanguage.googleapis.com",
            model="gemini-2.5-flash",
        ),
        reply=gemini_reply,
        cut_off=lambda text: json_response(
            {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "MAX_TOKENS"}]}
        ),
        stream=gemini_stream,
        auth_failure=lambda: gemini_error(
            403, "PERMISSION_DENIED", "The caller does not have permission"
        ),
        rate_limited=lambda: gemini_error(
            429,
            "RESOURCE_EXHAUSTED",
            "Quota exceeded",
            details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2s"}],
        ),
        server_error=lambda: gemini_error(503, "UNAVAILABLE", "The model is overloaded"),
        unknown_model=lambda: gemini_error(404, "NOT_FOUND", "models/nope is not found"),
        vectors=lambda rows: json_response({"embeddings": [{"values": list(row)} for row in rows]}),
        max_tokens_at=("generationConfig", "maxOutputTokens"),
        temperature_at=("generationConfig", "temperature"),
    )


PROVIDERS = {
    "openai": openai_flavor(ProviderKind.OPENAI),
    "openai-compatible": openai_flavor(ProviderKind.OPENAI_COMPATIBLE),
    "ollama": ollama_flavor(),
    "anthropic": anthropic_flavor(),
    "gemini": gemini_flavor(),
}
FLAVORS = [pytest.param(flavor, id=name) for name, flavor in PROVIDERS.items()]
EMBEDDING_FLAVORS = [
    pytest.param(flavor, id=name)
    for name, flavor in PROVIDERS.items()
    if flavor.vectors is not None
]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}

CONVERSATION = ChatRequest(
    messages=(
        ChatMessage(role=MessageRole.SYSTEM, content="SYS-PROMPT"),
        ChatMessage(role=MessageRole.USER, content="FIRST-USER"),
        ChatMessage(role=MessageRole.ASSISTANT, content="FIRST-REPLY"),
        ChatMessage(role=MessageRole.USER, content="SECOND-USER"),
    ),
    max_output_tokens=200,
    temperature=0.5,
)


def at(body: dict[str, Any], path: tuple[str, ...]) -> Any:
    for step in path:
        body = body[step]
    return body


def ask(text: str = "hello", **fields: Any) -> ChatRequest:
    return ChatRequest(messages=(ChatMessage(role=MessageRole.USER, content=text),), **fields)


@pytest.mark.parametrize("flavor", FLAVORS)
class TestEveryAdapterAnswersTheSameWay:
    async def test_a_conversation_comes_back_as_a_normalized_answer(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.reply("Four.", 21, 3))

        result = await flavor.adapter.chat(server.client, flavor.binding, CONVERSATION)

        assert result == ChatResult(
            text="Four.",
            finish_reason=FinishReason.STOP,
            usage=Usage(input_tokens=21, output_tokens=3),
        )

    async def test_an_answer_cut_by_the_limit_says_so(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.cut_off("Once upon"))

        result = await flavor.adapter.chat(server.client, flavor.binding, ask())

        assert result.text == "Once upon"
        assert result.finish_reason is FinishReason.LENGTH

    async def test_the_whole_conversation_is_sent_in_order(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.reply("Four.", 1, 1))

        await flavor.adapter.chat(server.client, flavor.binding, CONVERSATION)

        sent = server.request.content.decode()
        turns = [sent.index(text) for text in ("FIRST-USER", "FIRST-REPLY", "SECOND-USER")]
        assert turns == sorted(turns)
        # Where the system prompt goes is the provider's business: some lift it out of the turns.
        assert "SYS-PROMPT" in sent

    async def test_the_limits_of_the_caller_reach_the_provider_under_its_own_names(
        self, flavor: Flavor
    ) -> None:
        server = FakeServer(flavor.reply("Four.", 1, 1))

        await flavor.adapter.chat(server.client, flavor.binding, CONVERSATION)

        assert at(server.sent, flavor.max_tokens_at) == 200
        assert at(server.sent, flavor.temperature_at) == 0.5

    async def test_the_key_never_appears_in_the_address_or_the_body(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.reply("Four.", 1, 1), flavor.stream(["Hi"], 1, 1))

        await flavor.adapter.chat(server.client, flavor.binding, ask())
        whole = server.request
        await events(flavor.adapter.chat_stream(server.client, flavor.binding, ask()))
        streamed = server.request

        for request in (whole, streamed):
            assert KEY not in str(request.url)
            assert KEY not in request.content.decode()

    async def test_the_key_is_sent_once_in_a_header_when_there_is_one(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.reply("Four.", 1, 1))

        await flavor.adapter.chat(server.client, flavor.binding, ask())

        carrying = [name for name, value in server.request.headers.items() if KEY in value]
        assert len(carrying) == (1 if flavor.binding.api_key else 0)

    async def test_the_temperature_is_left_to_the_provider_when_nobody_sets_one(
        self, flavor: Flavor
    ) -> None:
        server = FakeServer(flavor.reply("Four.", 1, 1))

        await flavor.adapter.chat(server.client, flavor.binding, ask())

        with pytest.raises(KeyError):
            at(server.sent, flavor.temperature_at)


@pytest.mark.parametrize("flavor", FLAVORS)
class TestEveryAdapterStreamsTheSameWay:
    async def test_pieces_then_usage_then_finish(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.stream(["Fo", "ur", "."], 21, 3))

        got = await events(flavor.adapter.chat_stream(server.client, flavor.binding, CONVERSATION))

        assert got == [
            TextDelta(text="Fo"),
            TextDelta(text="ur"),
            TextDelta(text="."),
            UsageEvent(usage=Usage(input_tokens=21, output_tokens=3)),
            FinishEvent(reason=FinishReason.STOP),
        ]

    async def test_the_finish_is_always_the_last_event(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.stream(["a", "b"], 1, 1))

        got = await events(flavor.adapter.chat_stream(server.client, flavor.binding, ask()))

        assert isinstance(got[-1], FinishEvent)
        assert [type(event) for event in got].count(FinishEvent) == 1

    async def test_the_request_asks_for_a_stream_and_carries_the_same_limits(
        self, flavor: Flavor
    ) -> None:
        server = FakeServer(flavor.stream(["a"], 1, 1))

        await events(flavor.adapter.chat_stream(server.client, flavor.binding, CONVERSATION))

        assert at(server.sent, flavor.max_tokens_at) == 200
        assert at(server.sent, flavor.temperature_at) == 0.5

    async def test_a_failure_before_the_first_event_is_raised_and_not_streamed(
        self, flavor: Flavor
    ) -> None:
        server = FakeServer(flavor.rate_limited())

        with pytest.raises(RateLimitedError):
            await events(flavor.adapter.chat_stream(server.client, flavor.binding, ask()))


@pytest.mark.parametrize("flavor", FLAVORS)
class TestEveryAdapterFailsTheSameWay:
    async def test_a_refused_key(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.auth_failure())

        with pytest.raises(ProviderAuthError) as caught:
            await flavor.adapter.chat(server.client, flavor.binding, ask())

        assert not caught.value.retryable

    async def test_a_rate_limit_is_worth_retrying_and_carries_the_delay_asked_for(
        self, flavor: Flavor
    ) -> None:
        server = FakeServer(flavor.rate_limited())

        with pytest.raises(RateLimitedError) as caught:
            await flavor.adapter.chat(server.client, flavor.binding, ask())

        assert caught.value.retryable
        assert caught.value.retry_after_s == 2.0

    async def test_a_provider_in_trouble_is_worth_retrying(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.server_error())

        with pytest.raises(ProviderUnavailableError) as caught:
            await flavor.adapter.chat(server.client, flavor.binding, ask())

        assert caught.value.retryable

    async def test_an_unknown_model_is_a_request_that_will_never_work(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.unknown_model())

        with pytest.raises(InvalidRequestError) as caught:
            await flavor.adapter.chat(server.client, flavor.binding, ask())

        assert not caught.value.retryable

    async def test_errors_name_the_provider_and_the_status(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.server_error())

        with pytest.raises(ProviderUnavailableError) as caught:
            await flavor.adapter.chat(server.client, flavor.binding, ask())

        assert caught.value.details["provider"] == flavor.adapter.kind.value
        assert isinstance(caught.value.details["status"], int)

    async def test_a_server_that_cannot_be_reached(self, flavor: Flavor) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderUnavailableError) as caught:
            await flavor.adapter.chat(mock_client(refuse), flavor.binding, ask())

        assert caught.value.retryable

    async def test_a_server_that_does_not_answer_in_time(self, flavor: Flavor) -> None:
        def stall(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with pytest.raises(ProviderTimeoutError):
            await flavor.adapter.chat(mock_client(stall), flavor.binding, ask())

    async def test_an_answer_that_is_not_what_the_provider_speaks(self, flavor: Flavor) -> None:
        server = FakeServer(httpx.Response(200, content=b"<html>captive portal</html>"))

        with pytest.raises(ProviderUnavailableError):
            await flavor.adapter.chat(server.client, flavor.binding, ask())

    async def test_the_key_is_not_leaked_by_an_error_that_quotes_it(self, flavor: Flavor) -> None:
        quoting = httpx.Response(
            401,
            json={
                "error": {
                    "message": f"bad key {KEY}",
                    "type": "authentication_error",
                    "status": "X",
                }
            },
        )
        server = FakeServer(quoting)

        with pytest.raises(ProviderAuthError) as caught:
            await flavor.adapter.chat(server.client, flavor.binding, ask())

        assert KEY not in str(caught.value)
        assert KEY not in repr(caught.value.details)


@pytest.mark.parametrize("native", [True, False], ids=["native", "emulated"])
@pytest.mark.parametrize("flavor", FLAVORS)
class TestEveryAdapterGivesStructuredOutput:
    async def test_a_valid_json_answer_is_validated_and_returned_parsed(
        self, flavor: Flavor, native: bool
    ) -> None:
        server = FakeServer(flavor.reply('{"city": "Rome"}', 10, 5))
        request = ask("Where?", response_schema=SCHEMA)

        result = await run_structured(flavor.call(server), request, native=native)

        assert result.parsed == {"city": "Rome"}

    async def test_the_schema_reaches_the_model_one_way_or_another(
        self, flavor: Flavor, native: bool
    ) -> None:
        server = FakeServer(flavor.reply('{"city": "Rome"}', 10, 5))
        request = ask("Where?", response_schema=SCHEMA)

        await run_structured(flavor.call(server), request, native=native)

        assert "city" in server.request.content.decode()

    async def test_an_answer_that_does_not_fit_is_repaired_by_asking_again(
        self, flavor: Flavor, native: bool
    ) -> None:
        server = FakeServer(
            flavor.reply('{"town": "Rome"}', 10, 5), flavor.reply('{"city": "Rome"}', 20, 5)
        )
        request = ask("Where?", response_schema=SCHEMA)

        result = await run_structured(flavor.call(server), request, native=native)

        assert result.parsed == {"city": "Rome"}
        assert result.usage == Usage(input_tokens=30, output_tokens=10)
        assert len(server.requests) == 2
        assert '{\\"town\\": \\"Rome\\"}' in server.request.content.decode()


@pytest.mark.parametrize("flavor", EMBEDDING_FLAVORS)
class TestEveryAdapterEmbedsTheSameWay:
    async def test_one_vector_per_input_in_the_order_of_the_inputs(self, flavor: Flavor) -> None:
        assert flavor.vectors is not None
        server = FakeServer(flavor.vectors([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]))

        batch = await flavor.adapter.embed(server.client, flavor.binding, ["a", "b", "c"])

        assert batch.vectors == ((0.1, 0.2), (0.3, 0.4), (0.5, 0.6))

    async def test_the_inputs_are_sent_and_the_key_is_not_in_the_address(
        self, flavor: Flavor
    ) -> None:
        assert flavor.vectors is not None
        server = FakeServer(flavor.vectors([[0.1], [0.2]]))

        await flavor.adapter.embed(server.client, flavor.binding, ["INPUT-ONE", "INPUT-TWO"])

        sent = server.request.content.decode()
        assert sent.index("INPUT-ONE") < sent.index("INPUT-TWO")
        assert KEY not in str(server.request.url)
        assert KEY not in sent

    async def test_a_refused_key_is_the_same_error_as_for_chat(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.auth_failure())

        with pytest.raises(ProviderAuthError):
            await flavor.adapter.embed(server.client, flavor.binding, ["a"])

    async def test_a_provider_in_trouble_is_worth_retrying(self, flavor: Flavor) -> None:
        server = FakeServer(flavor.server_error())

        with pytest.raises(ProviderUnavailableError) as caught:
            await flavor.adapter.embed(server.client, flavor.binding, ["a"])

        assert caught.value.retryable


class TestAProviderWithoutEmbeddings:
    async def test_it_says_so_instead_of_pretending(self) -> None:
        flavor = anthropic_flavor()
        server = FakeServer()

        with pytest.raises(RoleNotSupportedError):
            await flavor.adapter.embed(server.client, flavor.binding, ["a"])

        assert server.requests == []


def test_every_provider_kind_has_an_adapter_in_this_suite() -> None:
    covered = {flavor.adapter.kind for flavor in PROVIDERS.values()}

    assert covered == set(ProviderKind)
