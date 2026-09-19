import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from factotum.contracts.chat import (
    ChatMessage,
    ChatRequest,
    EventUnion,
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
    ProviderUnavailableError,
    RateLimitedError,
)
from factotum.contracts.models import BindingOptions, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id
from factotum.model_gateway.providers.base import ResolvedBinding
from factotum.model_gateway.providers.openai import OpenAIAdapter
from factotum.model_gateway.testing import (
    collect_events,
    json_response,
    mock_client,
    resolved,
    sse_response,
)

KEY = "sk-live-0123456789abcdefghijklmnopqrstuv"
OFFICIAL = "https://api.openai.com/v1"
LOCAL_SERVER = "http://localhost:1234/v1"


def binding_for(
    kind: ProviderKind = ProviderKind.OPENAI,
    *,
    options: BindingOptions | None = None,
    api_key: str | None = KEY,
) -> ResolvedBinding:
    official = kind is ProviderKind.OPENAI
    binding = make_model_binding(
        tenant_id=make_tenant_id(),
        provider=kind,
        model="gpt-4o-mini" if official else "qwen2.5-7b-instruct",
        base_url=None if official else LOCAL_SERVER,
        secret_ref="env:OPENAI_API_KEY" if official else None,
        options=options,
    )
    return resolved(binding, base_url=OFFICIAL if official else LOCAL_SERVER, api_key=api_key)


def ask(text: str = "hello", **fields: Any) -> ChatRequest:
    return ChatRequest(messages=(ChatMessage(role=MessageRole.USER, content=text),), **fields)


class Wire:
    """A fake server: answers as told, one response per request, and keeps what it received."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)

    @property
    def client(self) -> httpx.AsyncClient:
        return mock_client(self)

    @property
    def request(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def sent(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.request.content)
        return body


def completion(
    content: str | None = "Hi there!",
    *,
    finish: str | None = "stop",
    refusal: str | None = None,
    usage: dict[str, int] | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
                "finish_reason": finish,
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return json_response(body)


def delta(
    content: str | None = None, *, finish: str | None = None, **extra: Any
) -> tuple[None, dict[str, Any]]:
    payload = {"content": content, **extra} if content is not None else dict(extra)
    return None, {"choices": [{"index": 0, "delta": payload, "finish_reason": finish}]}


DONE = (None, "[DONE]")
USAGE_CHUNK = (
    None,
    {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}},
)


async def events(stream: AsyncIterator[EventUnion]) -> list[EventUnion]:
    got, error = await collect_events(stream)
    if error is not None:
        raise error
    return got


def error_response(
    status: int, message: str, *, code: str | None = None, kind: str | None = None
) -> httpx.Response:
    return json_response(
        {"error": {"message": message, "type": kind, "param": None, "code": code}}, status=status
    )


class TestConstruction:
    @pytest.mark.parametrize("kind", [ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE])
    def test_it_serves_openai_and_compatible_servers(self, kind: ProviderKind) -> None:
        assert OpenAIAdapter(kind).kind is kind

    @pytest.mark.parametrize(
        "kind", [ProviderKind.OLLAMA, ProviderKind.ANTHROPIC, ProviderKind.GEMINI]
    )
    def test_other_providers_are_refused(self, kind: ProviderKind) -> None:
        with pytest.raises(ValueError, match="does not speak the OpenAI API"):
            OpenAIAdapter(kind)


class TestStructuredOutputSupport:
    def test_openai_supports_it_natively(self) -> None:
        assert OpenAIAdapter().native_structured_output(binding_for())

    def test_a_compatible_server_gets_it_emulated_unless_declared(self) -> None:
        adapter = OpenAIAdapter(ProviderKind.OPENAI_COMPATIBLE)

        assert not adapter.native_structured_output(binding_for(ProviderKind.OPENAI_COMPATIBLE))

    def test_a_compatible_server_can_declare_native_support(self) -> None:
        options = BindingOptions(native_structured_output=True)
        adapter = OpenAIAdapter(ProviderKind.OPENAI_COMPATIBLE)

        assert adapter.native_structured_output(
            binding_for(ProviderKind.OPENAI_COMPATIBLE, options=options)
        )

    def test_a_model_without_the_feature_can_opt_out(self) -> None:
        options = BindingOptions(native_structured_output=False)

        assert not OpenAIAdapter().native_structured_output(binding_for(options=options))


class TestChatRequest:
    async def test_the_request_goes_to_the_chat_completions_endpoint(self) -> None:
        wire = Wire(completion())

        await OpenAIAdapter().chat(wire.client, binding_for(), ask())

        assert wire.request.method == "POST"
        assert str(wire.request.url) == f"{OFFICIAL}/chat/completions"

    async def test_the_key_travels_only_in_the_authorization_header(self) -> None:
        wire = Wire(completion())

        await OpenAIAdapter().chat(wire.client, binding_for(), ask())

        assert wire.request.headers["authorization"] == f"Bearer {KEY}"
        assert KEY not in str(wire.request.url)
        assert KEY not in wire.request.content.decode()

    async def test_a_local_server_without_a_key_gets_no_authorization_header(self) -> None:
        wire = Wire(completion())
        local = binding_for(ProviderKind.OPENAI_COMPATIBLE, api_key=None)

        await OpenAIAdapter(ProviderKind.OPENAI_COMPATIBLE).chat(wire.client, local, ask())

        assert "authorization" not in wire.request.headers
        assert str(wire.request.url) == f"{LOCAL_SERVER}/chat/completions"

    async def test_the_model_is_the_one_of_the_binding_and_the_messages_keep_their_roles(
        self,
    ) -> None:
        wire = Wire(completion())
        request = ChatRequest(
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content="Be brief."),
                ChatMessage(role=MessageRole.USER, content="Hi"),
                ChatMessage(role=MessageRole.ASSISTANT, content="Hello!"),
                ChatMessage(role=MessageRole.USER, content="Again"),
            )
        )

        await OpenAIAdapter().chat(wire.client, binding_for(), request)

        assert wire.sent == {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
                {"role": "user", "content": "Again"},
            ],
        }

    async def test_nothing_is_sent_that_nobody_asked_for(self) -> None:
        wire = Wire(completion())

        await OpenAIAdapter().chat(wire.client, binding_for(), ask())

        assert set(wire.sent) == {"model", "messages"}

    async def test_openai_takes_max_completion_tokens(self) -> None:
        wire = Wire(completion())

        await OpenAIAdapter().chat(wire.client, binding_for(), ask(max_output_tokens=200))

        assert wire.sent["max_completion_tokens"] == 200
        assert "max_tokens" not in wire.sent

    async def test_a_compatible_server_takes_max_tokens(self) -> None:
        wire = Wire(completion())
        local = binding_for(ProviderKind.OPENAI_COMPATIBLE)

        await OpenAIAdapter(ProviderKind.OPENAI_COMPATIBLE).chat(
            wire.client, local, ask(max_output_tokens=200)
        )

        assert wire.sent["max_tokens"] == 200
        assert "max_completion_tokens" not in wire.sent

    async def test_the_cap_of_the_binding_holds_the_callers_limit(self) -> None:
        wire = Wire(completion())
        capped = binding_for(options=BindingOptions(max_output_tokens=100))

        await OpenAIAdapter().chat(wire.client, capped, ask(max_output_tokens=500))

        assert wire.sent["max_completion_tokens"] == 100

    async def test_the_cap_of_the_binding_applies_when_the_caller_sets_none(self) -> None:
        wire = Wire(completion())
        capped = binding_for(options=BindingOptions(max_output_tokens=100))

        await OpenAIAdapter().chat(wire.client, capped, ask())

        assert wire.sent["max_completion_tokens"] == 100

    async def test_the_temperature_of_the_caller_wins_over_the_binding(self) -> None:
        wire = Wire(completion(), completion())
        warm = binding_for(options=BindingOptions(temperature=0.9))

        await OpenAIAdapter().chat(wire.client, warm, ask(temperature=0.1))
        assert wire.sent["temperature"] == 0.1

        await OpenAIAdapter().chat(wire.client, warm, ask())
        assert wire.sent["temperature"] == 0.9

    async def test_a_zero_temperature_is_sent(self) -> None:
        wire = Wire(completion())

        await OpenAIAdapter().chat(wire.client, binding_for(), ask(temperature=0))

        assert wire.sent["temperature"] == 0


STRICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}


class TestStructuredRequest:
    async def test_a_schema_that_fits_strict_mode_asks_for_it(self) -> None:
        wire = Wire(completion('{"city": "Rome"}'))

        await OpenAIAdapter().chat(wire.client, binding_for(), ask(response_schema=STRICT_SCHEMA))

        assert wire.sent["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": STRICT_SCHEMA, "strict": True},
        }

    async def test_a_schema_outside_the_strict_subset_is_sent_without_strict(self) -> None:
        loose = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}
        wire = Wire(completion('{"n": 2}'))

        await OpenAIAdapter().chat(wire.client, binding_for(), ask(response_schema=loose))

        assert wire.sent["response_format"]["json_schema"] == {"name": "response", "schema": loose}

    async def test_strict_mode_needs_an_object_at_the_root(self) -> None:
        wire = Wire(completion('["a"]'))
        array = {"type": "array", "items": {"type": "string"}}

        await OpenAIAdapter().chat(wire.client, binding_for(), ask(response_schema=array))

        assert "strict" not in wire.sent["response_format"]["json_schema"]


class TestChatAnswer:
    async def test_text_finish_and_usage_are_normalized(self) -> None:
        answer = completion(
            "Hi there!", usage={"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}
        )

        result = await OpenAIAdapter().chat(Wire(answer).client, binding_for(), ask())

        assert result.text == "Hi there!"
        assert result.finish_reason is FinishReason.STOP
        assert result.usage == Usage(input_tokens=9, output_tokens=3)
        assert result.parsed is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("stop", FinishReason.STOP),
            ("length", FinishReason.LENGTH),
            ("content_filter", FinishReason.CONTENT_FILTER),
            ("tool_calls", FinishReason.OTHER),
            ("something_new", FinishReason.OTHER),
            (None, FinishReason.OTHER),
        ],
    )
    async def test_finish_reasons_are_mapped(self, raw: str | None, expected: FinishReason) -> None:
        result = await OpenAIAdapter().chat(
            Wire(completion(finish=raw)).client, binding_for(), ask()
        )

        assert result.finish_reason is expected

    async def test_a_server_that_reports_no_usage_gives_none(self) -> None:
        result = await OpenAIAdapter().chat(Wire(completion()).client, binding_for(), ask())

        assert result.usage is None

    async def test_a_refusal_is_a_content_filter_finish_with_the_refusal_as_text(self) -> None:
        answer = completion(None, refusal="I can't help with that.")

        result = await OpenAIAdapter().chat(Wire(answer).client, binding_for(), ask())

        assert result.text == "I can't help with that."
        assert result.finish_reason is FinishReason.CONTENT_FILTER

    async def test_a_null_content_is_an_empty_text(self) -> None:
        result = await OpenAIAdapter().chat(Wire(completion(None)).client, binding_for(), ask())

        assert result.text == ""

    @pytest.mark.parametrize(
        "body",
        [{}, {"choices": []}, {"choices": [{"finish_reason": "stop"}]}, ["not", "an", "object"]],
    )
    async def test_an_answer_of_another_shape_is_a_provider_failure(self, body: Any) -> None:
        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await OpenAIAdapter().chat(Wire(json_response(body)).client, binding_for(), ask())

    async def test_an_answer_that_is_not_json_is_a_provider_failure(self) -> None:
        html = httpx.Response(200, content=b"<html>Welcome to nginx</html>")

        with pytest.raises(ProviderUnavailableError, match="not valid JSON"):
            await OpenAIAdapter().chat(Wire(html).client, binding_for(), ask())

    async def test_unknown_fields_are_ignored(self) -> None:
        body = {
            "choices": [
                {
                    "message": {"content": "ok", "reasoning_content": "thinking...", "extra": 1},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
            "system_fingerprint": "fp_1",
        }

        result = await OpenAIAdapter().chat(Wire(json_response(body)).client, binding_for(), ask())

        assert result.text == "ok"


class TestErrors:
    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            (error_response(401, "Incorrect API key", code="invalid_api_key"), ProviderAuthError),
            (error_response(403, "Forbidden"), ProviderAuthError),
            (
                error_response(404, "The model does not exist", code="model_not_found"),
                InvalidRequestError,
            ),
            (
                error_response(400, "Unsupported parameter", kind="invalid_request_error"),
                InvalidRequestError,
            ),
            (
                error_response(429, "Rate limit reached", code="rate_limit_exceeded"),
                RateLimitedError,
            ),
            (error_response(500, "The server had an error"), ProviderUnavailableError),
            (error_response(503, "Overloaded"), ProviderUnavailableError),
        ],
    )
    async def test_statuses_become_typed_errors(
        self, response: httpx.Response, expected: type[Exception]
    ) -> None:
        with pytest.raises(expected):
            await OpenAIAdapter().chat(Wire(response).client, binding_for(), ask())

    async def test_the_provider_code_is_kept(self) -> None:
        response = error_response(404, "no such model", code="model_not_found")

        with pytest.raises(InvalidRequestError) as caught:
            await OpenAIAdapter().chat(Wire(response).client, binding_for(), ask())

        assert caught.value.details["provider_code"] == "model_not_found"
        assert caught.value.details["status"] == 404

    async def test_the_type_stands_in_for_a_missing_code(self) -> None:
        response = error_response(400, "bad", kind="invalid_request_error")

        with pytest.raises(InvalidRequestError) as caught:
            await OpenAIAdapter().chat(Wire(response).client, binding_for(), ask())

        assert caught.value.details["provider_code"] == "invalid_request_error"

    async def test_a_rate_limit_carries_what_the_server_asked_for(self) -> None:
        response = json_response(
            {"error": {"message": "slow down", "code": "rate_limit_exceeded"}},
            status=429,
            headers={"retry-after": "3"},
        )

        with pytest.raises(RateLimitedError) as caught:
            await OpenAIAdapter().chat(Wire(response).client, binding_for(), ask())

        assert caught.value.retry_after_s == 3.0

    async def test_an_exhausted_quota_is_not_a_rate_limit_because_waiting_will_not_help(
        self,
    ) -> None:
        response = error_response(429, "You exceeded your current quota", code="insufficient_quota")

        with pytest.raises(ProviderAuthError) as caught:
            await OpenAIAdapter().chat(Wire(response).client, binding_for(), ask())

        assert not caught.value.retryable
        assert caught.value.details["provider_code"] == "insufficient_quota"

    @pytest.mark.parametrize(
        "response",
        [
            error_response(
                400,
                "This model's maximum context length is 8192 tokens.",
                code="context_length_exceeded",
            ),
            # vLLM: the code is a number, only the message tells.
            json_response(
                {
                    "object": "error",
                    "message": "This model's maximum context length is 4096 tokens. "
                    "However, you requested 5000 tokens",
                    "type": "BadRequestError",
                    "code": 400,
                },
                status=400,
            ),
            # llama.cpp
            error_response(
                400,
                "the request exceeds the available context size, try increasing it",
                kind="exceed_context_size_error",
            ),
        ],
        ids=["openai", "vllm", "llama.cpp"],
    )
    async def test_a_prompt_that_does_not_fit_is_recognized_across_servers(
        self, response: httpx.Response
    ) -> None:
        with pytest.raises(ContextTooLongError):
            await OpenAIAdapter().chat(Wire(response).client, binding_for(), ask())

    async def test_the_plainer_error_shapes_of_compatible_servers_are_read(self) -> None:
        detail = json_response({"detail": "model not loaded"}, status=404)
        message = json_response({"message": "busy"}, status=503)
        text = json_response({"error": "out of memory"}, status=500)
        adapter = OpenAIAdapter(ProviderKind.OPENAI_COMPATIBLE)
        local = binding_for(ProviderKind.OPENAI_COMPATIBLE)

        with pytest.raises(InvalidRequestError, match="model not loaded"):
            await adapter.chat(Wire(detail).client, local, ask())
        with pytest.raises(ProviderUnavailableError, match="busy"):
            await adapter.chat(Wire(message).client, local, ask())
        with pytest.raises(ProviderUnavailableError, match="out of memory"):
            await adapter.chat(Wire(text).client, local, ask())

    async def test_a_body_that_is_not_json_is_still_reported(self) -> None:
        response = httpx.Response(502, content=b"<html>Bad Gateway</html>")

        with pytest.raises(ProviderUnavailableError, match="Bad Gateway"):
            await OpenAIAdapter().chat(Wire(response).client, binding_for(), ask())

    async def test_a_key_quoted_back_by_the_server_is_redacted(self) -> None:
        message = "Incorrect API key provided: sk-proj-abcdefghijklmnopqrstuvwxyz123456."
        response = error_response(401, message, code="invalid_api_key")

        with pytest.raises(ProviderAuthError) as caught:
            await OpenAIAdapter().chat(Wire(response).client, binding_for(), ask())

        assert "abcdefghijklmnop" not in str(caught.value)

    async def test_a_dead_server_is_a_provider_failure(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderUnavailableError):
            await OpenAIAdapter().chat(mock_client(refuse), binding_for(), ask())


class TestStreamingRequest:
    async def test_openai_is_asked_for_a_stream_with_usage(self) -> None:
        wire = Wire(sse_response(delta("Hi", finish="stop"), DONE))

        await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

        assert wire.sent["stream"] is True
        assert wire.sent["stream_options"] == {"include_usage": True}

    async def test_a_compatible_server_is_not_asked_for_usage_by_default(self) -> None:
        wire = Wire(sse_response(delta("Hi", finish="stop"), DONE))
        local = binding_for(ProviderKind.OPENAI_COMPATIBLE)

        await events(
            OpenAIAdapter(ProviderKind.OPENAI_COMPATIBLE).chat_stream(wire.client, local, ask())
        )

        assert wire.sent["stream"] is True
        assert "stream_options" not in wire.sent

    async def test_a_compatible_server_can_declare_it_reports_usage(self) -> None:
        wire = Wire(sse_response(delta("Hi", finish="stop"), DONE))
        local = binding_for(
            ProviderKind.OPENAI_COMPATIBLE, options=BindingOptions(stream_usage=True)
        )

        await events(
            OpenAIAdapter(ProviderKind.OPENAI_COMPATIBLE).chat_stream(wire.client, local, ask())
        )

        assert wire.sent["stream_options"] == {"include_usage": True}

    async def test_openai_can_be_told_not_to_ask(self) -> None:
        wire = Wire(sse_response(delta("Hi", finish="stop"), DONE))
        quiet = binding_for(options=BindingOptions(stream_usage=False))

        await events(OpenAIAdapter().chat_stream(wire.client, quiet, ask()))

        assert "stream_options" not in wire.sent

    async def test_a_plain_request_is_not_a_stream(self) -> None:
        wire = Wire(completion())

        await OpenAIAdapter().chat(wire.client, binding_for(), ask())

        assert "stream" not in wire.sent
        assert "stream_options" not in wire.sent


class TestStreamingAnswer:
    async def test_the_text_arrives_in_pieces_then_the_usage_then_the_finish(self) -> None:
        wire = Wire(
            sse_response(
                delta(role="assistant", content=""),
                delta("Hel"),
                delta("lo"),
                delta(finish="stop"),
                USAGE_CHUNK,
                DONE,
            )
        )

        got = await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

        assert got == [
            TextDelta(text="Hel"),
            TextDelta(text="lo"),
            UsageEvent(usage=Usage(input_tokens=12, output_tokens=3)),
            FinishEvent(reason=FinishReason.STOP),
        ]

    async def test_without_usage_there_is_no_usage_event(self) -> None:
        wire = Wire(sse_response(delta("Hi"), delta(finish="stop"), DONE))

        got = await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

        assert got == [TextDelta(text="Hi"), FinishEvent(reason=FinishReason.STOP)]

    async def test_a_cut_off_answer_says_so(self) -> None:
        wire = Wire(sse_response(delta("Once upon"), delta(finish="length"), DONE))

        got = await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

        assert got[-1] == FinishEvent(reason=FinishReason.LENGTH)

    async def test_a_refusal_is_streamed_and_finishes_as_a_content_filter(self) -> None:
        wire = Wire(
            sse_response(
                delta(refusal="I can't "), delta(refusal="help."), delta(finish="stop"), DONE
            )
        )

        got = await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

        assert got == [
            TextDelta(text="I can't "),
            TextDelta(text="help."),
            FinishEvent(reason=FinishReason.CONTENT_FILTER),
        ]

    async def test_a_server_that_omits_the_done_marker_is_fine_when_it_finished(self) -> None:
        wire = Wire(sse_response(delta("Hi"), delta(finish="stop")))

        got = await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

        assert got[-1] == FinishEvent(reason=FinishReason.STOP)

    async def test_a_server_that_omits_the_finish_reason_is_fine_when_it_said_done(self) -> None:
        wire = Wire(sse_response(delta("Hi"), DONE))

        got = await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

        assert got == [TextDelta(text="Hi"), FinishEvent(reason=FinishReason.OTHER)]

    async def test_a_stream_that_just_stops_is_a_provider_failure(self) -> None:
        wire = Wire(sse_response(delta("Hel"), delta("lo")))

        with pytest.raises(ProviderUnavailableError, match="before the answer was complete"):
            await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

    async def test_an_error_sent_inside_the_stream_is_raised(self) -> None:
        wire = Wire(
            sse_response(
                delta("Hel"),
                (None, {"error": {"message": "The server had an error", "type": "server_error"}}),
            )
        )

        seen, error = await collect_events(
            OpenAIAdapter().chat_stream(wire.client, binding_for(), ask())
        )

        assert seen == [TextDelta(text="Hel")]
        assert isinstance(error, ProviderUnavailableError)
        assert "The server had an error" in str(error)

    async def test_a_chunk_with_neither_choices_nor_an_error_is_skipped(self) -> None:
        wire = Wire(
            sse_response(
                (None, {"id": "chatcmpl-1", "object": "chat.completion.chunk"}), delta("Hi"), DONE
            )
        )

        got = await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

        assert got == [TextDelta(text="Hi"), FinishEvent(reason=FinishReason.OTHER)]

    async def test_a_chunk_that_is_not_json_is_a_provider_failure(self) -> None:
        wire = Wire(sse_response((None, "this is not json")))

        with pytest.raises(ProviderUnavailableError, match="not JSON"):
            await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

    async def test_an_error_status_is_raised_before_any_event(self) -> None:
        wire = Wire(error_response(429, "slow down", code="rate_limit_exceeded"))

        with pytest.raises(RateLimitedError):
            await events(OpenAIAdapter().chat_stream(wire.client, binding_for(), ask()))

    async def test_stopping_early_closes_the_stream_cleanly(self) -> None:
        wire = Wire(sse_response(delta("a"), delta("b"), delta("c"), delta(finish="stop"), DONE))
        stream = OpenAIAdapter().chat_stream(wire.client, binding_for(), ask())

        first = await anext(stream)
        await stream.aclose()

        assert first == TextDelta(text="a")


class TestEmbeddings:
    @staticmethod
    def vectors(*rows: list[float], usage: dict[str, int] | None = None) -> httpx.Response:
        body: dict[str, Any] = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": row}
                for index, row in enumerate(rows)
            ],
        }
        if usage is not None:
            body["usage"] = usage
        return json_response(body)

    async def test_the_request_goes_to_the_embeddings_endpoint(self) -> None:
        wire = Wire(self.vectors([0.1, 0.2], [0.3, 0.4]))
        embedding = binding_for()

        await OpenAIAdapter().embed(wire.client, embedding, ["first", "second"])

        assert str(wire.request.url) == f"{OFFICIAL}/embeddings"
        assert wire.request.headers["authorization"] == f"Bearer {KEY}"
        assert wire.sent == {"model": "gpt-4o-mini", "input": ["first", "second"]}

    async def test_dimensions_are_requested_only_when_the_user_declared_them(self) -> None:
        wire = Wire(self.vectors([0.1, 0.2]))
        sized = binding_for(options=BindingOptions(dimensions=256))

        await OpenAIAdapter().embed(wire.client, sized, ["text"])

        assert wire.sent["dimensions"] == 256

    async def test_vectors_and_usage_are_normalized(self) -> None:
        wire = Wire(
            self.vectors([0.1, 0.2], [0.3, 0.4], usage={"prompt_tokens": 5, "total_tokens": 5})
        )

        batch = await OpenAIAdapter().embed(wire.client, binding_for(), ["a", "b"])

        assert batch.vectors == ((0.1, 0.2), (0.3, 0.4))
        assert batch.usage == Usage(input_tokens=5)

    async def test_vectors_come_back_in_the_order_of_the_inputs_whatever_the_server_sent(
        self,
    ) -> None:
        shuffled = json_response(
            {
                "data": [
                    {"index": 2, "embedding": [3.0]},
                    {"index": 0, "embedding": [1.0]},
                    {"index": 1, "embedding": [2.0]},
                ]
            }
        )

        batch = await OpenAIAdapter().embed(Wire(shuffled).client, binding_for(), ["a", "b", "c"])

        assert batch.vectors == ((1.0,), (2.0,), (3.0,))

    async def test_a_server_that_sends_no_indexes_is_taken_in_order(self) -> None:
        plain = json_response({"data": [{"embedding": [1.0]}, {"embedding": [2.0]}]})

        batch = await OpenAIAdapter().embed(Wire(plain).client, binding_for(), ["a", "b"])

        assert batch.vectors == ((1.0,), (2.0,))

    async def test_integers_are_read_as_floats(self) -> None:
        batch = await OpenAIAdapter().embed(
            Wire(self.vectors([1, 0])).client,
            binding_for(),
            ["a"],
        )

        assert batch.vectors == ((1.0, 0.0),)

    @pytest.mark.parametrize(
        "body",
        [{}, {"data": "nope"}, {"data": [{"embedding": "AAAA"}]}, {"data": [{"index": 0}]}],
    )
    async def test_an_answer_of_another_shape_is_a_provider_failure(self, body: Any) -> None:
        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await OpenAIAdapter().embed(Wire(json_response(body)).client, binding_for(), ["a"])

    async def test_errors_are_mapped_like_the_chat_ones(self) -> None:
        response = error_response(401, "Incorrect API key", code="invalid_api_key")

        with pytest.raises(ProviderAuthError):
            await OpenAIAdapter().embed(Wire(response).client, binding_for(), ["a"])

    async def test_an_input_that_is_too_long_is_reported_as_such(self) -> None:
        response = error_response(
            400,
            "This model's maximum context length is 8191 tokens",
            code="context_length_exceeded",
        )

        with pytest.raises(ContextTooLongError):
            await OpenAIAdapter().embed(Wire(response).client, binding_for(), ["a"])
