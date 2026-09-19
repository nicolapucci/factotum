from typing import Any

import httpx
import pytest

from factotum.contracts.chat import (
    ChatMessage,
    ChatRequest,
    FinishEvent,
    FinishReason,
    MessageRole,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.errors import (
    ContextTooLongError,
    InvalidRequestError,
    ProviderAuthError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitedError,
    RoleNotSupportedError,
)
from factotum.contracts.models import BindingOptions, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id
from factotum.model_gateway.providers.anthropic import API_VERSION, AnthropicAdapter
from factotum.model_gateway.providers.base import ResolvedBinding
from factotum.model_gateway.testing import (
    FakeServer,
    ask,
    collect_events,
    events,
    json_response,
    mock_client,
    resolved,
    sse_response,
)

KEY = "sk-ant-api03-0123456789abcdefghijklmnopqrstuvwxyz"
BASE = "https://api.anthropic.com"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}


def binding_for(*, options: BindingOptions | None = None) -> ResolvedBinding:
    binding = make_model_binding(
        tenant_id=make_tenant_id(),
        provider=ProviderKind.ANTHROPIC,
        model="claude-sonnet-4-5",
        base_url=None,
        secret_ref="env:ANTHROPIC_API_KEY",
        options=options,
    )
    return resolved(binding, base_url=BASE, api_key=KEY)


def message(
    text: str = "Hello!",
    *,
    stop: str | None = "end_turn",
    usage: dict[str, int] | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": blocks if blocks is not None else [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 5},
    }
    return json_response(body)


def started(input_tokens: int = 25) -> tuple[str, dict[str, Any]]:
    return (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-sonnet-4-5",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        },
    )


def text_delta(text: str) -> tuple[str, dict[str, Any]]:
    return (
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
    )


def stopped(
    reason: str | None = "end_turn", *, usage: dict[str, int] | None = None
) -> tuple[str, dict[str, Any]]:
    return (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": usage if usage is not None else {"output_tokens": 15},
        },
    )


BLOCK_START = (
    "content_block_start",
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
)
BLOCK_STOP = ("content_block_stop", {"type": "content_block_stop", "index": 0})
PING = ("ping", {"type": "ping"})
END = ("message_stop", {"type": "message_stop"})


def stream_of(*texts: str, reason: str | None = "end_turn") -> httpx.Response:
    return sse_response(
        started(),
        BLOCK_START,
        PING,
        *(text_delta(text) for text in texts),
        BLOCK_STOP,
        stopped(reason),
        END,
    )


def error_response(
    status: int, kind: str, text: str, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    body = {"type": "error", "error": {"type": kind, "message": text}, "request_id": "req_011"}
    return json_response(body, status=status, headers=headers)


class TestConstruction:
    def test_it_is_the_anthropic_adapter(self) -> None:
        assert AnthropicAdapter().kind is ProviderKind.ANTHROPIC


class TestStructuredOutputSupport:
    def test_it_is_native_by_default(self) -> None:
        assert AnthropicAdapter().native_structured_output(binding_for())

    def test_a_model_without_the_feature_can_opt_out(self) -> None:
        options = BindingOptions(native_structured_output=False)

        assert not AnthropicAdapter().native_structured_output(binding_for(options=options))


class TestChatRequest:
    async def test_the_request_goes_to_the_messages_endpoint(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert server.request.method == "POST"
        assert str(server.request.url) == f"{BASE}/v1/messages"

    async def test_the_key_and_the_api_version_travel_in_headers(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert server.request.headers["x-api-key"] == KEY
        assert server.request.headers["anthropic-version"] == API_VERSION == "2023-06-01"
        assert "authorization" not in server.request.headers

    async def test_without_a_key_none_is_sent_and_the_api_will_say_so(self) -> None:
        keyless = resolved(binding_for().binding, base_url=BASE)
        server = FakeServer(
            error_response(401, "authentication_error", "x-api-key header is required")
        )

        with pytest.raises(ProviderAuthError):
            await AnthropicAdapter().chat(server.client, keyless, ask())

        assert "x-api-key" not in server.request.headers

    async def test_the_key_is_nowhere_else(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert KEY not in str(server.request.url)
        assert KEY not in server.request.content.decode()

    async def test_system_messages_are_lifted_out_of_the_conversation(self) -> None:
        server = FakeServer(message())
        request = ChatRequest(
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content="Be brief."),
                ChatMessage(role=MessageRole.SYSTEM, content="Answer in Italian."),
                ChatMessage(role=MessageRole.USER, content="Hi"),
                ChatMessage(role=MessageRole.ASSISTANT, content="Ciao!"),
                ChatMessage(role=MessageRole.USER, content="Again"),
            )
        )

        await AnthropicAdapter().chat(server.client, binding_for(), request)

        assert server.sent["system"] == "Be brief.\n\nAnswer in Italian."
        assert server.sent["messages"] == [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Ciao!"},
            {"role": "user", "content": "Again"},
        ]

    async def test_without_system_messages_there_is_no_system_field(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert "system" not in server.sent
        assert server.sent["model"] == "claude-sonnet-4-5"

    async def test_a_plain_request_is_not_a_stream_and_asks_for_nothing_extra(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert set(server.sent) == {"model", "max_tokens", "messages"}


class TestMaxTokens:
    """The API refuses a request without a limit."""

    async def test_there_is_a_default(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert server.sent["max_tokens"] == 4096

    async def test_the_limit_of_the_caller_is_used(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask(max_output_tokens=300))

        assert server.sent["max_tokens"] == 300

    async def test_the_cap_of_the_binding_holds_the_callers_limit(self) -> None:
        server = FakeServer(message())
        capped = binding_for(options=BindingOptions(max_output_tokens=100))

        await AnthropicAdapter().chat(server.client, capped, ask(max_output_tokens=500))

        assert server.sent["max_tokens"] == 100

    async def test_the_cap_of_the_binding_replaces_the_default_when_the_caller_sets_none(
        self,
    ) -> None:
        server = FakeServer(message())
        long = binding_for(options=BindingOptions(max_output_tokens=16_000))

        await AnthropicAdapter().chat(server.client, long, ask())

        assert server.sent["max_tokens"] == 16_000


class TestTemperature:
    @pytest.mark.parametrize(
        ("asked", "sent"), [(0, 0), (0.3, 0.3), (1.0, 1.0), (1.4, 1.0), (2, 1.0)]
    )
    async def test_it_is_passed_on_and_held_at_what_the_api_accepts(
        self, asked: float, sent: float
    ) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask(temperature=asked))

        assert server.sent["temperature"] == sent

    async def test_it_is_left_out_when_nobody_sets_one(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert "temperature" not in server.sent

    async def test_the_binding_supplies_one_when_the_caller_does_not(self) -> None:
        server = FakeServer(message())
        warm = binding_for(options=BindingOptions(temperature=0.8))

        await AnthropicAdapter().chat(server.client, warm, ask())

        assert server.sent["temperature"] == 0.8


class TestStructuredRequest:
    async def test_a_schema_in_the_basic_subset_is_enforced_by_the_api(self) -> None:
        server = FakeServer(message('{"city": "Rome"}'))

        await AnthropicAdapter().chat(server.client, binding_for(), ask(response_schema=SCHEMA))

        assert server.sent["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}

    async def test_the_model_is_told_what_is_wanted_as_well(self) -> None:
        server = FakeServer(message('{"city": "Rome"}'))
        request = ChatRequest(
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content="Be brief."),
                ChatMessage(role=MessageRole.USER, content="Where?"),
            ),
            response_schema=SCHEMA,
        )

        await AnthropicAdapter().chat(server.client, binding_for(), request)

        assert server.sent["system"].startswith("Be brief.\n\n")
        assert '"city"' in server.sent["system"]
        assert server.sent["messages"] == [{"role": "user", "content": "Where?"}]

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}},
            {"type": "array", "items": {"type": "string"}},
            {"type": "object", "properties": {"a": {"$ref": "#/$defs/x"}}, "$defs": {}},
        ],
        ids=["loose-object", "array-at-the-root", "refs"],
    )
    async def test_any_other_schema_is_left_to_the_prompt(self, schema: dict[str, Any]) -> None:
        server = FakeServer(message("{}"))

        await AnthropicAdapter().chat(server.client, binding_for(), ask(response_schema=schema))

        assert "output_config" not in server.sent
        assert "JSON Schema" in server.sent["system"]

    async def test_a_plain_request_has_no_output_config_and_no_instructions(self) -> None:
        server = FakeServer(message())

        await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert "output_config" not in server.sent
        assert "system" not in server.sent


class TestChatAnswer:
    async def test_text_finish_and_usage_are_normalized(self) -> None:
        server = FakeServer(message("Hello!", usage={"input_tokens": 12, "output_tokens": 4}))

        result = await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert result.text == "Hello!"
        assert result.finish_reason is FinishReason.STOP
        assert result.usage == Usage(input_tokens=12, output_tokens=4)
        assert result.parsed is None

    async def test_only_the_text_blocks_make_the_answer(self) -> None:
        blocks: list[dict[str, Any]] = [
            {"type": "thinking", "thinking": "Hmm...", "signature": "abc"},
            {"type": "text", "text": "Part one. "},
            {"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}},
            {"type": "text", "text": "Part two."},
        ]

        result = await AnthropicAdapter().chat(
            FakeServer(message(blocks=blocks)).client, binding_for(), ask()
        )

        assert result.text == "Part one. Part two."

    async def test_an_answer_without_blocks_is_an_empty_text(self) -> None:
        result = await AnthropicAdapter().chat(
            FakeServer(message(blocks=[], stop="refusal")).client, binding_for(), ask()
        )

        assert result.text == ""
        assert result.finish_reason is FinishReason.CONTENT_FILTER

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("end_turn", FinishReason.STOP),
            ("stop_sequence", FinishReason.STOP),
            ("max_tokens", FinishReason.LENGTH),
            ("model_context_window_exceeded", FinishReason.LENGTH),
            ("refusal", FinishReason.CONTENT_FILTER),
            ("tool_use", FinishReason.OTHER),
            ("pause_turn", FinishReason.OTHER),
            (None, FinishReason.OTHER),
        ],
    )
    async def test_stop_reasons_are_mapped(self, raw: str | None, expected: FinishReason) -> None:
        result = await AnthropicAdapter().chat(
            FakeServer(message(stop=raw)).client, binding_for(), ask()
        )

        assert result.finish_reason is expected

    async def test_an_answer_that_reports_no_usage_gives_none(self) -> None:
        body = {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}

        result = await AnthropicAdapter().chat(
            FakeServer(json_response(body)).client, binding_for(), ask()
        )

        assert result.usage is None

    @pytest.mark.parametrize(
        "body", [{}, {"content": "hello"}, {"content": [{"text": "x"}]}, ["x"]]
    )
    async def test_an_answer_of_another_shape_is_a_provider_failure(self, body: Any) -> None:
        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await AnthropicAdapter().chat(
                FakeServer(json_response(body)).client, binding_for(), ask()
            )

    async def test_an_answer_that_is_not_json_is_a_provider_failure(self) -> None:
        server = FakeServer(httpx.Response(200, content=b"<html>hi</html>"))

        with pytest.raises(ProviderUnavailableError, match="not valid JSON"):
            await AnthropicAdapter().chat(server.client, binding_for(), ask())


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "kind", "expected"),
        [
            (400, "invalid_request_error", InvalidRequestError),
            (401, "authentication_error", ProviderAuthError),
            (403, "permission_error", ProviderAuthError),
            (404, "not_found_error", InvalidRequestError),
            (413, "request_too_large", ContextTooLongError),
            (429, "rate_limit_error", RateLimitedError),
            (500, "api_error", ProviderUnavailableError),
            (529, "overloaded_error", ProviderUnavailableError),
        ],
    )
    async def test_statuses_become_typed_errors(
        self, status: int, kind: str, expected: type[Exception]
    ) -> None:
        server = FakeServer(error_response(status, kind, "trouble"))

        with pytest.raises(expected):
            await AnthropicAdapter().chat(server.client, binding_for(), ask())

    async def test_the_error_type_is_kept_as_the_provider_code(self) -> None:
        server = FakeServer(error_response(404, "not_found_error", "model: claude-nope"))

        with pytest.raises(InvalidRequestError, match="claude-nope") as caught:
            await AnthropicAdapter().chat(server.client, binding_for(), ask())

        assert caught.value.details == {
            "provider": "anthropic",
            "status": 404,
            "provider_code": "not_found_error",
        }

    async def test_a_rate_limit_carries_what_the_server_asked_for(self) -> None:
        response = error_response(
            429, "rate_limit_error", "slow down", headers={"retry-after": "12"}
        )

        with pytest.raises(RateLimitedError) as caught:
            await AnthropicAdapter().chat(FakeServer(response).client, binding_for(), ask())

        assert caught.value.retry_after_s == 12.0
        assert caught.value.retryable

    async def test_an_overloaded_api_is_worth_retrying(self) -> None:
        response = error_response(529, "overloaded_error", "Overloaded")

        with pytest.raises(ProviderUnavailableError) as caught:
            await AnthropicAdapter().chat(FakeServer(response).client, binding_for(), ask())

        assert caught.value.retryable

    async def test_a_prompt_that_does_not_fit_is_a_context_error_although_the_status_is_400(
        self,
    ) -> None:
        response = error_response(
            400, "invalid_request_error", "prompt is too long: 250000 tokens > 200000 maximum"
        )

        with pytest.raises(ContextTooLongError) as caught:
            await AnthropicAdapter().chat(FakeServer(response).client, binding_for(), ask())

        assert not caught.value.retryable

    async def test_an_empty_balance_is_not_retried_because_waiting_will_not_help(self) -> None:
        response = error_response(
            400,
            "invalid_request_error",
            "Your credit balance is too low to access the Anthropic API.",
        )

        with pytest.raises(ProviderAuthError) as caught:
            await AnthropicAdapter().chat(FakeServer(response).client, binding_for(), ask())

        assert not caught.value.retryable

    async def test_a_key_quoted_back_by_the_server_is_redacted(self) -> None:
        response = error_response(401, "authentication_error", f"invalid x-api-key: {KEY}")

        with pytest.raises(ProviderAuthError) as caught:
            await AnthropicAdapter().chat(FakeServer(response).client, binding_for(), ask())

        assert KEY not in str(caught.value)

    async def test_a_body_that_is_not_the_usual_envelope_is_still_reported(self) -> None:
        response = httpx.Response(502, content=b"<html>Bad Gateway</html>")

        with pytest.raises(ProviderUnavailableError, match="Bad Gateway"):
            await AnthropicAdapter().chat(FakeServer(response).client, binding_for(), ask())

    async def test_a_dead_server_is_a_provider_failure(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderUnavailableError):
            await AnthropicAdapter().chat(mock_client(refuse), binding_for(), ask())


class TestStreamingRequest:
    async def test_the_request_asks_for_a_stream(self) -> None:
        server = FakeServer(stream_of("Hi"))

        await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

        assert server.sent["stream"] is True
        assert str(server.request.url) == f"{BASE}/v1/messages"
        assert server.request.headers["x-api-key"] == KEY


class TestStreamingAnswer:
    async def test_the_text_arrives_in_pieces_then_the_usage_then_the_finish(self) -> None:
        server = FakeServer(stream_of("Hel", "lo"))

        got = await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [
            TextDelta(text="Hel"),
            TextDelta(text="lo"),
            UsageEvent(usage=Usage(input_tokens=25, output_tokens=15)),
            FinishEvent(reason=FinishReason.STOP),
        ]

    async def test_the_output_counter_is_a_running_total_and_the_last_one_counts(self) -> None:
        server = FakeServer(
            sse_response(
                started(input_tokens=25),
                text_delta("Hi"),
                stopped("end_turn", usage={"output_tokens": 42}),
                END,
            )
        )

        got = await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

        assert UsageEvent(usage=Usage(input_tokens=25, output_tokens=42)) in got

    async def test_a_final_counter_that_repeats_the_input_replaces_it(self) -> None:
        server = FakeServer(
            sse_response(
                started(input_tokens=25),
                text_delta("Hi"),
                stopped("end_turn", usage={"input_tokens": 30, "output_tokens": 8}),
                END,
            )
        )

        got = await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

        assert UsageEvent(usage=Usage(input_tokens=30, output_tokens=8)) in got

    async def test_what_is_not_text_is_ignored(self) -> None:
        server = FakeServer(
            sse_response(
                started(),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "Hmm"},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "input_json_delta", "partial_json": '{"a"'},
                    },
                ),
                text_delta(""),
                text_delta("Answer"),
                stopped(),
                END,
            )
        )

        got = await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

        assert [event for event in got if isinstance(event, TextDelta)] == [
            TextDelta(text="Answer")
        ]

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("end_turn", FinishReason.STOP),
            ("max_tokens", FinishReason.LENGTH),
            ("refusal", FinishReason.CONTENT_FILTER),
            (None, FinishReason.OTHER),
        ],
    )
    async def test_the_stop_reason_becomes_the_finish(
        self, reason: str | None, expected: FinishReason
    ) -> None:
        server = FakeServer(stream_of("Hi", reason=reason))

        got = await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got[-1] == FinishEvent(reason=expected)

    async def test_a_stream_without_counters_has_no_usage_event(self) -> None:
        bare_start = ("message_start", {"type": "message_start", "message": {"id": "msg_1"}})
        bare_stop = (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        )
        server = FakeServer(sse_response(bare_start, text_delta("Hi"), bare_stop, END))

        got = await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [TextDelta(text="Hi"), FinishEvent(reason=FinishReason.STOP)]

    async def test_a_stream_that_stops_before_the_end_is_a_provider_failure(self) -> None:
        server = FakeServer(sse_response(started(), text_delta("Hel")))

        with pytest.raises(ProviderUnavailableError, match="before the answer was complete"):
            await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_an_event_that_is_not_json_is_a_provider_failure(self) -> None:
        server = FakeServer(sse_response(("message_start", "nope")))

        with pytest.raises(ProviderUnavailableError, match="not JSON"):
            await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_an_event_of_another_shape_is_a_provider_failure(self) -> None:
        server = FakeServer(sse_response(("mystery", {"no": "type"})))

        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_events_of_a_kind_that_does_not_exist_yet_are_ignored(self) -> None:
        future = ("brand_new", {"type": "brand_new", "payload": 1})
        server = FakeServer(sse_response(started(), future, text_delta("Hi"), stopped(), END))

        got = await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

        assert TextDelta(text="Hi") in got

    async def test_an_error_status_is_raised_before_any_event(self) -> None:
        server = FakeServer(error_response(429, "rate_limit_error", "slow down"))

        with pytest.raises(RateLimitedError):
            await events(AnthropicAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_stopping_early_closes_the_stream_cleanly(self) -> None:
        server = FakeServer(stream_of("a", "b", "c"))
        stream = AnthropicAdapter().chat_stream(server.client, binding_for(), ask())

        first = await anext(stream)
        await stream.aclose()

        assert first == TextDelta(text="a")


class TestErrorEvents:
    """An error in the middle of a stream arrives with a 200: only its type says what it is."""

    @staticmethod
    def failing(kind: str | None, text: str | None = "trouble") -> httpx.Response:
        error: dict[str, Any] = {}
        if kind is not None:
            error["type"] = kind
        if text is not None:
            error["message"] = text
        return sse_response(
            started(), text_delta("Hel"), ("error", {"type": "error", "error": error})
        )

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("overloaded_error", ProviderUnavailableError),
            ("api_error", ProviderUnavailableError),
            ("rate_limit_error", RateLimitedError),
            ("authentication_error", ProviderAuthError),
            ("permission_error", ProviderAuthError),
            ("invalid_request_error", InvalidRequestError),
            ("timeout_error", ProviderTimeoutError),
            ("request_too_large", ContextTooLongError),
            ("something_new", ProviderUnavailableError),
        ],
    )
    async def test_the_type_decides_the_kind_of_error(
        self, kind: str, expected: type[Exception]
    ) -> None:
        server = FakeServer(self.failing(kind))

        seen, error = await collect_events(
            AnthropicAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert seen == [TextDelta(text="Hel")]
        assert type(error) is expected

    async def test_the_message_and_the_type_are_kept(self) -> None:
        server = FakeServer(self.failing("overloaded_error", "Overloaded"))

        _, error = await collect_events(
            AnthropicAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert error is not None
        assert "Overloaded" in str(error)
        assert error.details["provider_code"] == "overloaded_error"

    async def test_the_words_can_say_more_than_the_type(self) -> None:
        server = FakeServer(self.failing("invalid_request_error", "prompt is too long: 9 > 8"))

        _, error = await collect_events(
            AnthropicAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert type(error) is ContextTooLongError

    async def test_an_error_event_with_nothing_in_it_is_still_an_error(self) -> None:
        server = FakeServer(self.failing(None, None))

        _, error = await collect_events(
            AnthropicAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert type(error) is ProviderUnavailableError

    async def test_an_error_event_without_an_error_body_is_still_an_error(self) -> None:
        server = FakeServer(sse_response(started(), ("error", {"type": "error"})))

        _, error = await collect_events(
            AnthropicAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert type(error) is ProviderUnavailableError


class TestEmbeddings:
    async def test_anthropic_has_none_and_says_where_to_look(self) -> None:
        server = FakeServer()

        with pytest.raises(RoleNotSupportedError, match="another provider"):
            await AnthropicAdapter().embed(server.client, binding_for(), ["text"])

        assert server.requests == []
