import json
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
    ContentFilteredError,
    ContextTooLongError,
    InvalidRequestError,
    ProviderAuthError,
    ProviderUnavailableError,
    RateLimitedError,
)
from factotum.contracts.models import BindingOptions, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id
from factotum.model_gateway.providers.base import ResolvedBinding
from factotum.model_gateway.providers.gemini import GeminiAdapter
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

KEY = "AIzaSyD0123456789abcdefghijklmnopqrstuv"
BASE = "https://generativelanguage.googleapis.com"
GENERATE = f"{BASE}/v1beta/models/gemini-2.5-flash:generateContent"
STREAM = f"{BASE}/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
BATCH_EMBED = f"{BASE}/v1beta/models/gemini-2.5-flash:batchEmbedContents"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "days": {"type": "integer", "minimum": 1}},
    "required": ["city"],
}


def binding_for(
    *, model: str = "gemini-2.5-flash", options: BindingOptions | None = None
) -> ResolvedBinding:
    binding = make_model_binding(
        tenant_id=make_tenant_id(),
        provider=ProviderKind.GEMINI,
        model=model,
        base_url=None,
        secret_ref="env:GEMINI_API_KEY",
        options=options,
    )
    return resolved(binding, base_url=BASE, api_key=KEY)


def answer(
    text: str = "Hello!",
    *,
    finish: str | None = "STOP",
    usage: dict[str, int] | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> httpx.Response:
    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": parts if parts is not None else [{"text": text}]},
        "index": 0,
    }
    if finish is not None:
        candidate["finishReason"] = finish
    body: dict[str, Any] = {
        "candidates": [candidate],
        "modelVersion": "gemini-2.5-flash",
        "responseId": "r1",
    }
    body["usageMetadata"] = (
        usage
        if usage is not None
        else {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}
    )
    return json_response(body)


def chunk(
    text: str | None = None,
    *,
    finish: str | None = None,
    usage: dict[str, int] | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> tuple[None, dict[str, Any]]:
    candidate: dict[str, Any] = {"index": 0}
    if parts is not None:
        candidate["content"] = {"role": "model", "parts": parts}
    elif text is not None:
        candidate["content"] = {"role": "model", "parts": [{"text": text}]}
    if finish is not None:
        candidate["finishReason"] = finish
    body: dict[str, Any] = {"candidates": [candidate], "modelVersion": "gemini-2.5-flash"}
    if usage is not None:
        body["usageMetadata"] = usage
    return None, body


LAST_USAGE = {"promptTokenCount": 8, "candidatesTokenCount": 2, "totalTokenCount": 10}


def error_response(
    status: int,
    text: str,
    name: str = "INVALID_ARGUMENT",
    *,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    error: dict[str, Any] = {"code": status, "message": text, "status": name}
    if details is not None:
        error["details"] = details
    return json_response({"error": error}, status=status, headers=headers)


class TestConstruction:
    def test_it_is_the_gemini_adapter(self) -> None:
        assert GeminiAdapter().kind is ProviderKind.GEMINI


class TestStructuredOutputSupport:
    def test_it_is_native_by_default(self) -> None:
        assert GeminiAdapter().native_structured_output(binding_for())

    def test_a_model_without_the_feature_can_opt_out(self) -> None:
        options = BindingOptions(native_structured_output=False)

        assert not GeminiAdapter().native_structured_output(binding_for(options=options))


class TestChatRequest:
    async def test_the_request_goes_to_the_generate_content_endpoint_of_the_model(self) -> None:
        server = FakeServer(answer())

        await GeminiAdapter().chat(server.client, binding_for(), ask())

        assert server.request.method == "POST"
        assert str(server.request.url) == GENERATE

    @pytest.mark.parametrize(
        ("model", "path"),
        [
            ("gemini-2.5-flash", "models/gemini-2.5-flash"),
            ("models/gemini-2.5-flash", "models/gemini-2.5-flash"),
            ("tunedModels/my-tune-1", "tunedModels/my-tune-1"),
        ],
    )
    async def test_the_model_is_named_the_way_the_api_wants(self, model: str, path: str) -> None:
        server = FakeServer(answer())

        await GeminiAdapter().chat(server.client, binding_for(model=model), ask())

        assert str(server.request.url) == f"{BASE}/v1beta/{path}:generateContent"

    async def test_the_key_travels_only_in_the_header_and_never_in_the_url(self) -> None:
        server = FakeServer(answer())

        await GeminiAdapter().chat(server.client, binding_for(), ask())

        assert server.request.headers["x-goog-api-key"] == KEY
        assert "authorization" not in server.request.headers
        assert KEY not in str(server.request.url)
        assert "key=" not in str(server.request.url)
        assert KEY not in server.request.content.decode()

    async def test_without_a_key_none_is_sent_and_the_api_will_say_so(self) -> None:
        keyless = resolved(binding_for().binding, base_url=BASE)
        server = FakeServer(
            error_response(403, "Method doesn't allow unregistered callers", "PERMISSION_DENIED")
        )

        with pytest.raises(ProviderAuthError):
            await GeminiAdapter().chat(server.client, keyless, ask())

        assert "x-goog-api-key" not in server.request.headers

    async def test_roles_are_user_and_model_and_the_system_messages_become_the_instruction(
        self,
    ) -> None:
        server = FakeServer(answer())
        request = ChatRequest(
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content="Be brief."),
                ChatMessage(role=MessageRole.SYSTEM, content="Answer in Italian."),
                ChatMessage(role=MessageRole.USER, content="Hi"),
                ChatMessage(role=MessageRole.ASSISTANT, content="Ciao!"),
                ChatMessage(role=MessageRole.USER, content="Again"),
            )
        )

        await GeminiAdapter().chat(server.client, binding_for(), request)

        assert server.sent["systemInstruction"] == {
            "parts": [{"text": "Be brief."}, {"text": "Answer in Italian."}]
        }
        assert server.sent["contents"] == [
            {"role": "user", "parts": [{"text": "Hi"}]},
            {"role": "model", "parts": [{"text": "Ciao!"}]},
            {"role": "user", "parts": [{"text": "Again"}]},
        ]

    async def test_a_plain_request_asks_for_nothing_extra(self) -> None:
        server = FakeServer(answer())

        await GeminiAdapter().chat(server.client, binding_for(), ask("Hi"))

        assert server.sent == {"contents": [{"role": "user", "parts": [{"text": "Hi"}]}]}


class TestGenerationConfig:
    async def test_the_output_limit_and_the_temperature_are_sent(self) -> None:
        server = FakeServer(answer())

        await GeminiAdapter().chat(
            server.client, binding_for(), ask(max_output_tokens=300, temperature=0.4)
        )

        assert server.sent["generationConfig"] == {"maxOutputTokens": 300, "temperature": 0.4}

    async def test_the_cap_of_the_binding_holds_the_callers_limit(self) -> None:
        server = FakeServer(answer())
        capped = binding_for(options=BindingOptions(max_output_tokens=100))

        await GeminiAdapter().chat(server.client, capped, ask(max_output_tokens=500))

        assert server.sent["generationConfig"]["maxOutputTokens"] == 100

    async def test_the_binding_supplies_a_temperature_when_the_caller_does_not(self) -> None:
        server = FakeServer(answer())
        warm = binding_for(options=BindingOptions(temperature=0.8))

        await GeminiAdapter().chat(server.client, warm, ask())

        assert server.sent["generationConfig"] == {"temperature": 0.8}

    @pytest.mark.parametrize("temperature", [0, 2])
    async def test_the_whole_range_of_the_api_is_passed_through(self, temperature: float) -> None:
        server = FakeServer(answer())

        await GeminiAdapter().chat(server.client, binding_for(), ask(temperature=temperature))

        assert server.sent["generationConfig"]["temperature"] == temperature


class TestStructuredRequest:
    async def test_a_schema_the_api_documents_is_enforced_by_it(self) -> None:
        server = FakeServer(answer('{"city": "Rome"}'))

        await GeminiAdapter().chat(server.client, binding_for(), ask(response_schema=SCHEMA))

        config = server.sent["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseJsonSchema"] == SCHEMA

    async def test_the_model_is_told_what_is_wanted_as_well(self) -> None:
        server = FakeServer(answer('{"city": "Rome"}'))
        request = ChatRequest(
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content="Be brief."),
                ChatMessage(role=MessageRole.USER, content="Where?"),
            ),
            response_schema=SCHEMA,
        )

        await GeminiAdapter().chat(server.client, binding_for(), request)

        parts = server.sent["systemInstruction"]["parts"]
        assert parts[0] == {"text": "Be brief."}
        assert '"city"' in parts[1]["text"]

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {"a": {"type": "string", "minLength": 1}}},
            {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}}},
            {"type": "object", "properties": {"a": {"type": "string", "default": "x"}}},
            {"type": "array", "items": {"type": "string", "maxLength": 3}},
            {"type": "object", "properties": {"a": {"allOf": [{"type": "string"}]}}},
        ],
        ids=["minLength", "pattern", "default", "items-maxLength", "allOf"],
    )
    async def test_a_schema_outside_the_documented_subset_is_left_to_the_prompt(
        self, schema: dict[str, Any]
    ) -> None:
        server = FakeServer(answer("{}"))

        await GeminiAdapter().chat(server.client, binding_for(), ask(response_schema=schema))

        assert "generationConfig" not in server.sent
        assert "JSON Schema" in server.sent["systemInstruction"]["parts"][0]["text"]

    async def test_a_property_named_like_an_unsupported_keyword_is_only_a_name(self) -> None:
        schema = {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "default": {"type": "string"}},
            "required": ["pattern"],
        }
        server = FakeServer(answer("{}"))

        await GeminiAdapter().chat(server.client, binding_for(), ask(response_schema=schema))

        assert server.sent["generationConfig"]["responseJsonSchema"] == schema

    async def test_references_and_definitions_are_within_the_subset(self) -> None:
        schema = {
            "type": "object",
            "properties": {"home": {"$ref": "#/$defs/place"}},
            "$defs": {"place": {"type": "object", "properties": {"name": {"type": "string"}}}},
        }
        server = FakeServer(answer("{}"))

        await GeminiAdapter().chat(server.client, binding_for(), ask(response_schema=schema))

        assert "responseJsonSchema" in server.sent["generationConfig"]


class TestChatAnswer:
    async def test_text_finish_and_usage_are_normalized(self) -> None:
        server = FakeServer(answer("Hello!"))

        result = await GeminiAdapter().chat(server.client, binding_for(), ask())

        assert result.text == "Hello!"
        assert result.finish_reason is FinishReason.STOP
        assert result.usage == Usage(input_tokens=10, output_tokens=5)
        assert result.parsed is None

    async def test_the_parts_of_the_answer_are_joined_and_thoughts_are_left_out(self) -> None:
        parts: list[dict[str, Any]] = [
            {"text": "Let me think about it.", "thought": True},
            {"text": "Part one. "},
            {"text": "Part two."},
        ]

        result = await GeminiAdapter().chat(
            FakeServer(answer(parts=parts)).client, binding_for(), ask()
        )

        assert result.text == "Part one. Part two."

    async def test_thinking_counts_as_output_because_it_is_generated_and_billed(self) -> None:
        usage = {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 40}

        result = await GeminiAdapter().chat(
            FakeServer(answer(usage=usage)).client, binding_for(), ask()
        )

        assert result.usage == Usage(input_tokens=10, output_tokens=45)

    async def test_an_answer_of_only_thinking_counts_that_much(self) -> None:
        usage = {"promptTokenCount": 10, "thoughtsTokenCount": 40}

        result = await GeminiAdapter().chat(
            FakeServer(answer(usage=usage)).client, binding_for(), ask()
        )

        assert result.usage == Usage(input_tokens=10, output_tokens=40)

    async def test_an_answer_that_reports_only_the_prompt_size(self) -> None:
        result = await GeminiAdapter().chat(
            FakeServer(answer(usage={"promptTokenCount": 10})).client, binding_for(), ask()
        )

        assert result.usage == Usage(input_tokens=10, output_tokens=None)

    async def test_an_answer_without_usage_gives_none(self) -> None:
        body = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}

        result = await GeminiAdapter().chat(
            FakeServer(json_response(body)).client, binding_for(), ask()
        )

        assert result.usage is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("STOP", FinishReason.STOP),
            ("MAX_TOKENS", FinishReason.LENGTH),
            ("SAFETY", FinishReason.CONTENT_FILTER),
            ("RECITATION", FinishReason.CONTENT_FILTER),
            ("BLOCKLIST", FinishReason.CONTENT_FILTER),
            ("PROHIBITED_CONTENT", FinishReason.CONTENT_FILTER),
            ("SPII", FinishReason.CONTENT_FILTER),
            ("IMAGE_SAFETY", FinishReason.CONTENT_FILTER),
            ("LANGUAGE", FinishReason.OTHER),
            ("OTHER", FinishReason.OTHER),
            ("MALFORMED_FUNCTION_CALL", FinishReason.OTHER),
            (None, FinishReason.OTHER),
        ],
    )
    async def test_finish_reasons_are_mapped(self, raw: str | None, expected: FinishReason) -> None:
        result = await GeminiAdapter().chat(
            FakeServer(answer(finish=raw)).client, binding_for(), ask()
        )

        assert result.finish_reason is expected

    async def test_a_candidate_stopped_by_the_filters_may_have_no_content_at_all(self) -> None:
        body = {"candidates": [{"finishReason": "SAFETY", "index": 0}]}

        result = await GeminiAdapter().chat(
            FakeServer(json_response(body)).client, binding_for(), ask()
        )

        assert result.text == ""
        assert result.finish_reason is FinishReason.CONTENT_FILTER

    async def test_a_prompt_the_api_refuses_to_answer_is_reported_with_the_reason(self) -> None:
        body = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}

        with pytest.raises(ContentFilteredError, match="PROHIBITED_CONTENT") as caught:
            await GeminiAdapter().chat(FakeServer(json_response(body)).client, binding_for(), ask())

        assert caught.value.details["block_reason"] == "PROHIBITED_CONTENT"

    async def test_feedback_alongside_a_normal_answer_is_not_a_refusal(self) -> None:
        body = {
            "candidates": [{"content": {"parts": [{"text": "fine"}]}, "finishReason": "STOP"}],
            "promptFeedback": {"blockReason": "BLOCK_REASON_UNSPECIFIED"},
        }

        result = await GeminiAdapter().chat(
            FakeServer(json_response(body)).client, binding_for(), ask()
        )

        assert result.text == "fine"

    @pytest.mark.parametrize("body", [{}, {"candidates": []}, {"promptFeedback": {}}])
    async def test_an_answer_with_nothing_in_it_is_a_provider_failure(self, body: Any) -> None:
        with pytest.raises(ProviderUnavailableError, match="no candidates"):
            await GeminiAdapter().chat(FakeServer(json_response(body)).client, binding_for(), ask())

    @pytest.mark.parametrize(
        "body", [["nope"], {"candidates": "x"}, {"candidates": [{"content": 3}]}]
    )
    async def test_an_answer_of_another_shape_is_a_provider_failure(self, body: Any) -> None:
        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await GeminiAdapter().chat(FakeServer(json_response(body)).client, binding_for(), ask())

    async def test_an_answer_that_is_not_json_is_a_provider_failure(self) -> None:
        server = FakeServer(httpx.Response(200, content=b"<html>hi</html>"))

        with pytest.raises(ProviderUnavailableError, match="not valid JSON"):
            await GeminiAdapter().chat(server.client, binding_for(), ask())


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "name", "expected"),
        [
            (400, "INVALID_ARGUMENT", InvalidRequestError),
            (400, "FAILED_PRECONDITION", InvalidRequestError),
            (403, "PERMISSION_DENIED", ProviderAuthError),
            (404, "NOT_FOUND", InvalidRequestError),
            (429, "RESOURCE_EXHAUSTED", RateLimitedError),
            (500, "INTERNAL", ProviderUnavailableError),
            (503, "UNAVAILABLE", ProviderUnavailableError),
            (504, "DEADLINE_EXCEEDED", ProviderUnavailableError),
        ],
    )
    async def test_statuses_become_typed_errors(
        self, status: int, name: str, expected: type[Exception]
    ) -> None:
        server = FakeServer(error_response(status, "trouble", name))

        with pytest.raises(expected):
            await GeminiAdapter().chat(server.client, binding_for(), ask())

    async def test_the_status_name_is_kept_as_the_provider_code(self) -> None:
        server = FakeServer(error_response(404, "models/nope is not found", "NOT_FOUND"))

        with pytest.raises(InvalidRequestError, match="models/nope") as caught:
            await GeminiAdapter().chat(server.client, binding_for(), ask())

        assert caught.value.details == {
            "provider": "gemini",
            "status": 404,
            "provider_code": "NOT_FOUND",
        }

    async def test_a_bad_key_is_an_auth_error_although_the_api_calls_it_a_bad_argument(
        self,
    ) -> None:
        server = FakeServer(error_response(400, "API key not valid. Please pass a valid API key."))

        with pytest.raises(ProviderAuthError) as caught:
            await GeminiAdapter().chat(server.client, binding_for(), ask())

        assert not caught.value.retryable

    async def test_an_expired_key_is_an_auth_error_too(self) -> None:
        server = FakeServer(error_response(400, "API key expired. Please renew the API key."))

        with pytest.raises(ProviderAuthError):
            await GeminiAdapter().chat(server.client, binding_for(), ask())

    async def test_a_prompt_that_does_not_fit_is_a_context_error(self) -> None:
        text = (
            "The input token count (1200000) exceeds the maximum number of tokens allowed "
            "(1048575)."
        )
        server = FakeServer(error_response(400, text))

        with pytest.raises(ContextTooLongError):
            await GeminiAdapter().chat(server.client, binding_for(), ask())

    async def test_a_rate_limit_carries_the_delay_the_api_attached(self) -> None:
        details: list[dict[str, Any]] = [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"},
        ]
        server = FakeServer(error_response(429, "quota", "RESOURCE_EXHAUSTED", details=details))

        with pytest.raises(RateLimitedError) as caught:
            await GeminiAdapter().chat(server.client, binding_for(), ask())

        assert caught.value.retry_after_s == 34.0

    async def test_a_fractional_delay_is_understood(self) -> None:
        details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0.5s"}]
        server = FakeServer(error_response(429, "quota", "RESOURCE_EXHAUSTED", details=details))

        with pytest.raises(RateLimitedError) as caught:
            await GeminiAdapter().chat(server.client, binding_for(), ask())

        assert caught.value.retry_after_s == 0.5

    async def test_the_retry_after_header_is_used_when_there_is_no_retry_info(self) -> None:
        response = error_response(429, "quota", "RESOURCE_EXHAUSTED", headers={"retry-after": "7"})

        with pytest.raises(RateLimitedError) as caught:
            await GeminiAdapter().chat(FakeServer(response).client, binding_for(), ask())

        assert caught.value.retry_after_s == 7.0

    @pytest.mark.parametrize(
        "details",
        [None, [], [{"@type": "x"}], [{"retryDelay": "soon"}], [{"retryDelay": "1m"}]],
    )
    async def test_no_usable_delay_means_no_hint(
        self, details: list[dict[str, Any]] | None
    ) -> None:
        server = FakeServer(error_response(429, "quota", "RESOURCE_EXHAUSTED", details=details))

        with pytest.raises(RateLimitedError) as caught:
            await GeminiAdapter().chat(server.client, binding_for(), ask())

        assert caught.value.retry_after_s is None

    async def test_a_body_that_is_not_the_usual_envelope_is_still_reported(self) -> None:
        response = httpx.Response(502, content=b"<html>Bad Gateway</html>")

        with pytest.raises(ProviderUnavailableError, match="Bad Gateway"):
            await GeminiAdapter().chat(FakeServer(response).client, binding_for(), ask())

    async def test_a_key_quoted_back_by_the_server_is_redacted(self) -> None:
        response = error_response(403, f"The key {KEY} is not allowed", "PERMISSION_DENIED")

        with pytest.raises(ProviderAuthError) as caught:
            await GeminiAdapter().chat(FakeServer(response).client, binding_for(), ask())

        assert KEY not in str(caught.value)

    async def test_a_dead_server_is_a_provider_failure(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderUnavailableError):
            await GeminiAdapter().chat(mock_client(refuse), binding_for(), ask())


class TestStreamingRequest:
    async def test_the_request_goes_to_the_stream_endpoint_asking_for_server_sent_events(
        self,
    ) -> None:
        server = FakeServer(sse_response(chunk("Hi", finish="STOP", usage=LAST_USAGE)))

        await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert str(server.request.url) == STREAM

    async def test_the_key_is_still_only_in_the_header(self) -> None:
        server = FakeServer(sse_response(chunk("Hi", finish="STOP")))

        await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert server.request.headers["x-goog-api-key"] == KEY
        assert KEY not in str(server.request.url)

    async def test_the_body_is_the_same_as_for_a_whole_answer(self) -> None:
        server = FakeServer(sse_response(chunk("Hi", finish="STOP")))

        await events(
            GeminiAdapter().chat_stream(server.client, binding_for(), ask("Hi", temperature=0.2))
        )

        assert server.sent == {
            "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
            "generationConfig": {"temperature": 0.2},
        }


class TestStreamingAnswer:
    async def test_the_text_arrives_in_pieces_then_the_usage_then_the_finish(self) -> None:
        server = FakeServer(
            sse_response(chunk("Hel"), chunk("lo", finish="STOP", usage=LAST_USAGE))
        )

        got = await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [
            TextDelta(text="Hel"),
            TextDelta(text="lo"),
            UsageEvent(usage=Usage(input_tokens=8, output_tokens=2)),
            FinishEvent(reason=FinishReason.STOP),
        ]

    async def test_the_last_usage_reported_is_the_one_that_counts(self) -> None:
        early = {"promptTokenCount": 8, "candidatesTokenCount": 1}
        server = FakeServer(
            sse_response(chunk("Hel", usage=early), chunk("lo", finish="STOP", usage=LAST_USAGE))
        )

        got = await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert UsageEvent(usage=Usage(input_tokens=8, output_tokens=2)) in got

    async def test_thoughts_and_empty_pieces_are_not_text(self) -> None:
        thinking = [{"text": "Considering...", "thought": True}]
        server = FakeServer(
            sse_response(
                chunk(parts=thinking),
                chunk(""),
                chunk("Answer"),
                chunk(finish="STOP"),
            )
        )

        got = await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert [event for event in got if isinstance(event, TextDelta)] == [
            TextDelta(text="Answer")
        ]

    async def test_a_chunk_with_only_the_usage_is_skipped(self) -> None:
        only_usage = (None, {"usageMetadata": LAST_USAGE})
        server = FakeServer(sse_response(chunk("Hi", finish="STOP"), only_usage))

        got = await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [
            TextDelta(text="Hi"),
            UsageEvent(usage=Usage(input_tokens=8, output_tokens=2)),
            FinishEvent(reason=FinishReason.STOP),
        ]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("STOP", FinishReason.STOP),
            ("MAX_TOKENS", FinishReason.LENGTH),
            ("SAFETY", FinishReason.CONTENT_FILTER),
            ("OTHER", FinishReason.OTHER),
        ],
    )
    async def test_the_finish_reason_is_mapped(self, raw: str, expected: FinishReason) -> None:
        server = FakeServer(sse_response(chunk("Hi"), chunk(finish=raw)))

        got = await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got[-1] == FinishEvent(reason=expected)

    async def test_an_answer_stopped_by_the_filters_keeps_what_came_before(self) -> None:
        server = FakeServer(sse_response(chunk("Once upon"), chunk(finish="SAFETY")))

        got = await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [
            TextDelta(text="Once upon"),
            FinishEvent(reason=FinishReason.CONTENT_FILTER),
        ]

    async def test_a_blocked_prompt_raises_before_any_text(self) -> None:
        blocked = (None, {"promptFeedback": {"blockReason": "SAFETY"}})
        server = FakeServer(sse_response(blocked))

        seen, error = await collect_events(
            GeminiAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert seen == []
        assert isinstance(error, ContentFilteredError)

    async def test_a_stream_without_counters_has_no_usage_event(self) -> None:
        server = FakeServer(sse_response(chunk("Hi", finish="STOP")))

        got = await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [TextDelta(text="Hi"), FinishEvent(reason=FinishReason.STOP)]

    async def test_a_stream_that_stops_before_a_finish_reason_is_a_provider_failure(self) -> None:
        server = FakeServer(sse_response(chunk("Hel"), chunk("lo")))

        with pytest.raises(ProviderUnavailableError, match="before the answer was complete"):
            await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_a_chunk_that_is_not_json_is_a_provider_failure(self) -> None:
        server = FakeServer(sse_response((None, "not json")))

        with pytest.raises(ProviderUnavailableError, match="not JSON"):
            await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_a_chunk_of_another_shape_is_a_provider_failure(self) -> None:
        server = FakeServer(sse_response((None, {"candidates": "x"})))

        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_an_error_status_is_raised_before_any_event(self) -> None:
        server = FakeServer(error_response(429, "quota", "RESOURCE_EXHAUSTED"))

        with pytest.raises(RateLimitedError):
            await events(GeminiAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_stopping_early_closes_the_stream_cleanly(self) -> None:
        server = FakeServer(sse_response(chunk("a"), chunk("b"), chunk("c", finish="STOP")))
        stream = GeminiAdapter().chat_stream(server.client, binding_for(), ask())

        first = await anext(stream)
        await stream.aclose()

        assert first == TextDelta(text="a")


class TestErrorChunks:
    """An error in the middle of a stream arrives as a chunk: its own code says what it is."""

    @staticmethod
    def failing(code: int, text: str, name: str) -> httpx.Response:
        error = {"error": {"code": code, "message": text, "status": name}}
        return sse_response(chunk("Hel"), (None, error))

    @pytest.mark.parametrize(
        ("code", "name", "expected"),
        [
            (503, "UNAVAILABLE", ProviderUnavailableError),
            (500, "INTERNAL", ProviderUnavailableError),
            (429, "RESOURCE_EXHAUSTED", RateLimitedError),
            (403, "PERMISSION_DENIED", ProviderAuthError),
            (400, "INVALID_ARGUMENT", InvalidRequestError),
        ],
    )
    async def test_the_code_decides_the_kind_of_error(
        self, code: int, name: str, expected: type[Exception]
    ) -> None:
        server = FakeServer(self.failing(code, "trouble", name))

        seen, error = await collect_events(
            GeminiAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert seen == [TextDelta(text="Hel")]
        assert type(error) is expected

    async def test_the_message_and_the_status_name_are_kept(self) -> None:
        server = FakeServer(self.failing(503, "The model is overloaded.", "UNAVAILABLE"))

        _, error = await collect_events(
            GeminiAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert error is not None
        assert "overloaded" in str(error)
        assert error.details["provider_code"] == "UNAVAILABLE"

    async def test_an_error_without_a_code_is_taken_as_the_providers_fault(self) -> None:
        body = {"error": {"message": "something broke"}}
        server = FakeServer(sse_response((None, body)))

        _, error = await collect_events(
            GeminiAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert type(error) is ProviderUnavailableError


class TestEmbeddings:
    @staticmethod
    def vectors(*rows: list[float]) -> httpx.Response:
        return json_response({"embeddings": [{"values": row} for row in rows]})

    async def test_the_request_goes_to_the_batch_endpoint_with_one_entry_per_input(self) -> None:
        server = FakeServer(self.vectors([0.1, 0.2], [0.3, 0.4]))

        await GeminiAdapter().embed(server.client, binding_for(), ["first", "second"])

        assert str(server.request.url) == BATCH_EMBED
        assert server.request.headers["x-goog-api-key"] == KEY
        assert server.sent == {
            "requests": [
                {"model": "models/gemini-2.5-flash", "content": {"parts": [{"text": "first"}]}},
                {"model": "models/gemini-2.5-flash", "content": {"parts": [{"text": "second"}]}},
            ]
        }

    async def test_the_size_is_requested_only_when_the_user_declared_it(self) -> None:
        server = FakeServer(self.vectors([0.1]))
        sized = binding_for(options=BindingOptions(dimensions=256))

        await GeminiAdapter().embed(server.client, sized, ["text"])

        assert server.sent["requests"][0]["outputDimensionality"] == 256

    async def test_vectors_are_normalized_and_there_is_no_usage_to_report(self) -> None:
        server = FakeServer(self.vectors([0.1, 0.2], [0.3, 0.4]))

        batch = await GeminiAdapter().embed(server.client, binding_for(), ["a", "b"])

        assert batch.vectors == ((0.1, 0.2), (0.3, 0.4))
        assert batch.usage is None

    async def test_a_large_batch_is_split_at_what_the_api_takes_and_keeps_its_order(self) -> None:
        inputs = [f"text {i}" for i in range(250)]

        def handler(request: httpx.Request) -> httpx.Response:
            asked = json.loads(request.content)["requests"]
            sizes.append(len(asked))
            # The vector of an input is [its number]: order can be checked at the end.
            numbers = [[float(item["content"]["parts"][0]["text"].split()[1])] for item in asked]
            return self.vectors(*numbers)

        sizes: list[int] = []
        batch = await GeminiAdapter().embed(mock_client(handler), binding_for(), inputs)

        assert sizes == [100, 100, 50]
        assert batch.vectors == tuple((float(i),) for i in range(250))

    async def test_a_failure_in_a_later_part_of_the_batch_is_raised(self) -> None:
        server = FakeServer(
            self.vectors(*([[0.1]] * 100)), error_response(503, "overloaded", "UNAVAILABLE")
        )

        with pytest.raises(ProviderUnavailableError):
            await GeminiAdapter().embed(server.client, binding_for(), ["x"] * 101)

    @pytest.mark.parametrize(
        "body", [{}, {"embeddings": []}, {"embeddings": [{"values": "x"}]}, {"embeddings": [{}]}]
    )
    async def test_an_answer_of_another_shape_is_a_provider_failure(self, body: Any) -> None:
        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await GeminiAdapter().embed(
                FakeServer(json_response(body)).client, binding_for(), ["a"]
            )

    async def test_errors_are_mapped_like_the_chat_ones(self) -> None:
        server = FakeServer(error_response(400, "API key not valid. Please pass a valid API key."))

        with pytest.raises(ProviderAuthError):
            await GeminiAdapter().embed(server.client, binding_for(), ["a"])
