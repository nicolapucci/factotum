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
    ProviderUnavailableError,
)
from factotum.contracts.models import BindingOptions, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id
from factotum.model_gateway.providers.base import ResolvedBinding
from factotum.model_gateway.providers.ollama import OllamaAdapter
from factotum.model_gateway.testing import (
    FakeServer,
    ask,
    collect_events,
    events,
    json_response,
    mock_client,
    ndjson_response,
    resolved,
)

BASE = "http://localhost:11434"


def binding_for(
    *, options: BindingOptions | None = None, api_key: str | None = None
) -> ResolvedBinding:
    binding = make_model_binding(
        tenant_id=make_tenant_id(),
        provider=ProviderKind.OLLAMA,
        model="llama3.1:8b",
        base_url=BASE,
        options=options,
    )
    return resolved(binding, api_key=api_key)


def answer(
    content: str = "Hi there!",
    *,
    reason: str | None = "stop",
    prompt: int | None = 9,
    output: int | None = 3,
) -> httpx.Response:
    body: dict[str, Any] = {
        "model": "llama3.1:8b",
        "created_at": "2025-01-01T10:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "total_duration": 5_000_000_000,
        "load_duration": 3_000_000,
    }
    if reason is not None:
        body["done_reason"] = reason
    if prompt is not None:
        body["prompt_eval_count"] = prompt
    if output is not None:
        body["eval_count"] = output
    return json_response(body)


def piece(content: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "model": "llama3.1:8b",
        "created_at": "2025-01-01T10:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": False,
        **extra,
    }


FINAL = piece(done=True, done_reason="stop", prompt_eval_count=12, eval_count=3)


def error_response(status: int, message: str) -> httpx.Response:
    return json_response({"error": message}, status=status)


class TestConstruction:
    def test_it_is_the_ollama_adapter(self) -> None:
        assert OllamaAdapter().kind is ProviderKind.OLLAMA


class TestStructuredOutputSupport:
    def test_ollama_constrains_the_output_natively(self) -> None:
        assert OllamaAdapter().native_structured_output(binding_for())

    def test_a_model_without_the_feature_can_opt_out(self) -> None:
        options = BindingOptions(native_structured_output=False)

        assert not OllamaAdapter().native_structured_output(binding_for(options=options))


class TestChatRequest:
    async def test_the_request_goes_to_the_native_chat_endpoint(self) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(), ask())

        assert server.request.method == "POST"
        assert str(server.request.url) == f"{BASE}/api/chat"

    async def test_the_answer_is_asked_whole_because_ollama_streams_by_default(self) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(), ask())

        assert server.sent["stream"] is False

    async def test_the_model_and_the_messages_are_sent_with_their_roles(self) -> None:
        server = FakeServer(answer())
        request = ChatRequest(
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content="Be brief."),
                ChatMessage(role=MessageRole.USER, content="Hi"),
                ChatMessage(role=MessageRole.ASSISTANT, content="Hello!"),
                ChatMessage(role=MessageRole.USER, content="Again"),
            )
        )

        await OllamaAdapter().chat(server.client, binding_for(), request)

        assert server.sent["model"] == "llama3.1:8b"
        assert server.sent["messages"] == [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Again"},
        ]

    async def test_a_local_server_gets_no_authorization_header(self) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(), ask())

        assert "authorization" not in server.request.headers

    async def test_a_key_is_sent_as_a_bearer_token_for_a_proxy_or_the_hosted_service(self) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(api_key="proxy-token"), ask())

        assert server.request.headers["authorization"] == "Bearer proxy-token"
        assert "proxy-token" not in str(server.request.url)
        assert "proxy-token" not in server.request.content.decode()


class TestContextWindow:
    """Ollama's default window is small and it truncates the prompt without a word."""

    async def test_the_window_always_covers_the_prompt_the_binding_allows_and_an_answer(
        self,
    ) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(), ask())

        assert server.sent["options"]["num_ctx"] == 8192 + 2048

    async def test_the_window_follows_what_the_user_declared_as_input_size(self) -> None:
        server = FakeServer(answer())
        big = binding_for(options=BindingOptions(max_input_tokens=32_000))

        await OllamaAdapter().chat(server.client, big, ask())

        assert server.sent["options"]["num_ctx"] == 32_000 + 2048

    async def test_the_answer_cap_of_the_binding_sets_the_room_for_the_answer(self) -> None:
        server = FakeServer(answer())
        capped = binding_for(options=BindingOptions(max_input_tokens=4096, max_output_tokens=512))

        await OllamaAdapter().chat(server.client, capped, ask())

        assert server.sent["options"]["num_ctx"] == 4096 + 512

    async def test_a_declared_window_is_used_as_it_is(self) -> None:
        server = FakeServer(answer())
        declared = binding_for(options=BindingOptions(num_ctx=16_384))

        await OllamaAdapter().chat(server.client, declared, ask())

        assert server.sent["options"]["num_ctx"] == 16_384

    async def test_the_window_does_not_change_with_the_request_because_a_change_reloads_the_model(
        self,
    ) -> None:
        server = FakeServer(answer(), answer(), answer())
        binding = binding_for()

        await OllamaAdapter().chat(server.client, binding, ask(max_output_tokens=50))
        first = server.sent["options"]["num_ctx"]
        await OllamaAdapter().chat(server.client, binding, ask(max_output_tokens=900))
        second = server.sent["options"]["num_ctx"]
        await OllamaAdapter().chat(server.client, binding, ask(temperature=0.3))
        third = server.sent["options"]["num_ctx"]

        assert first == second == third


class TestGenerationOptions:
    async def test_only_the_window_is_sent_when_nothing_else_was_asked(self) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(), ask())

        assert set(server.sent["options"]) == {"num_ctx"}

    async def test_the_output_limit_becomes_num_predict(self) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(), ask(max_output_tokens=200))

        assert server.sent["options"]["num_predict"] == 200

    async def test_the_cap_of_the_binding_holds_the_callers_limit(self) -> None:
        server = FakeServer(answer())
        capped = binding_for(options=BindingOptions(max_output_tokens=100))

        await OllamaAdapter().chat(server.client, capped, ask(max_output_tokens=500))

        assert server.sent["options"]["num_predict"] == 100

    async def test_the_temperature_of_the_caller_wins_over_the_binding(self) -> None:
        server = FakeServer(answer(), answer())
        warm = binding_for(options=BindingOptions(temperature=0.9))

        await OllamaAdapter().chat(server.client, warm, ask(temperature=0.1))
        assert server.sent["options"]["temperature"] == 0.1

        await OllamaAdapter().chat(server.client, warm, ask())
        assert server.sent["options"]["temperature"] == 0.9

    async def test_a_zero_temperature_is_sent(self) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(), ask(temperature=0))

        assert server.sent["options"]["temperature"] == 0


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}


class TestStructuredRequest:
    async def test_the_schema_is_sent_as_the_format(self) -> None:
        server = FakeServer(answer('{"city": "Rome"}'))

        await OllamaAdapter().chat(server.client, binding_for(), ask(response_schema=SCHEMA))

        assert server.sent["format"] == SCHEMA

    async def test_the_model_is_also_told_what_is_wanted(self) -> None:
        server = FakeServer(answer('{"city": "Rome"}'))
        request = ChatRequest(
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content="Be brief."),
                ChatMessage(role=MessageRole.USER, content="Where?"),
            ),
            response_schema=SCHEMA,
        )

        await OllamaAdapter().chat(server.client, binding_for(), request)

        system, user = server.sent["messages"]
        assert system["role"] == "system"
        assert system["content"].startswith("Be brief.")
        assert '"city"' in system["content"]
        assert user == {"role": "user", "content": "Where?"}

    async def test_a_plain_request_has_no_format_and_no_extra_instructions(self) -> None:
        server = FakeServer(answer())

        await OllamaAdapter().chat(server.client, binding_for(), ask("Where?"))

        assert "format" not in server.sent
        assert server.sent["messages"] == [{"role": "user", "content": "Where?"}]


class TestChatAnswer:
    async def test_text_finish_and_usage_are_normalized(self) -> None:
        server = FakeServer(answer("Hi there!", prompt=9, output=3))

        result = await OllamaAdapter().chat(server.client, binding_for(), ask())

        assert result.text == "Hi there!"
        assert result.finish_reason is FinishReason.STOP
        assert result.usage == Usage(input_tokens=9, output_tokens=3)
        assert result.parsed is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("stop", FinishReason.STOP),
            ("length", FinishReason.LENGTH),
            ("load", FinishReason.OTHER),
            ("unload", FinishReason.OTHER),
            (None, FinishReason.STOP),  # older servers send no reason
        ],
    )
    async def test_finish_reasons_are_mapped(self, raw: str | None, expected: FinishReason) -> None:
        server = FakeServer(answer(reason=raw))

        result = await OllamaAdapter().chat(server.client, binding_for(), ask())

        assert result.finish_reason is expected

    async def test_a_server_that_reports_no_counters_gives_no_usage(self) -> None:
        server = FakeServer(answer(prompt=None, output=None))

        result = await OllamaAdapter().chat(server.client, binding_for(), ask())

        assert result.usage is None

    async def test_the_thinking_of_a_reasoning_model_is_not_part_of_the_answer(self) -> None:
        body = {
            "message": {"role": "assistant", "content": "42", "thinking": "Let me think..."},
            "done": True,
            "done_reason": "stop",
        }

        result = await OllamaAdapter().chat(
            FakeServer(json_response(body)).client, binding_for(), ask()
        )

        assert result.text == "42"

    @pytest.mark.parametrize("body", [{}, {"done": True}, {"message": "hello"}, ["nope"]])
    async def test_an_answer_of_another_shape_is_a_provider_failure(self, body: Any) -> None:
        server = FakeServer(json_response(body))

        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await OllamaAdapter().chat(server.client, binding_for(), ask())

    async def test_an_error_in_the_body_of_a_good_answer_is_raised(self) -> None:
        server = FakeServer(json_response({"error": "the model runner has terminated"}))

        with pytest.raises(ProviderUnavailableError, match="runner has terminated"):
            await OllamaAdapter().chat(server.client, binding_for(), ask())

    async def test_an_answer_that_is_not_json_is_a_provider_failure(self) -> None:
        server = FakeServer(httpx.Response(200, content=b"Ollama is running"))

        with pytest.raises(ProviderUnavailableError, match="not valid JSON"):
            await OllamaAdapter().chat(server.client, binding_for(), ask())


class TestErrors:
    async def test_an_unknown_model_is_an_invalid_request(self) -> None:
        response = error_response(404, "model 'nope' not found, try pulling it first")

        with pytest.raises(InvalidRequestError, match="try pulling it first") as caught:
            await OllamaAdapter().chat(FakeServer(response).client, binding_for(), ask())

        assert caught.value.details["status"] == 404

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, InvalidRequestError),
            (401, ProviderAuthError),
            (403, ProviderAuthError),
            (500, ProviderUnavailableError),
            (503, ProviderUnavailableError),
        ],
    )
    async def test_statuses_become_typed_errors(
        self, status: int, expected: type[Exception]
    ) -> None:
        with pytest.raises(expected):
            await OllamaAdapter().chat(
                FakeServer(error_response(status, "trouble")).client, binding_for(), ask()
            )

    async def test_an_input_that_does_not_fit_is_a_context_error(self) -> None:
        response = error_response(400, "the input length exceeds the context length")

        with pytest.raises(ContextTooLongError):
            await OllamaAdapter().embed(FakeServer(response).client, binding_for(), ["text"])

    async def test_a_body_that_is_not_json_is_still_reported(self) -> None:
        response = httpx.Response(502, content=b"<html>Bad Gateway</html>")

        with pytest.raises(ProviderUnavailableError, match="Bad Gateway"):
            await OllamaAdapter().chat(FakeServer(response).client, binding_for(), ask())

    async def test_a_server_that_is_not_running_is_a_provider_failure(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderUnavailableError, match="ConnectError"):
            await OllamaAdapter().chat(mock_client(refuse), binding_for(), ask())


class TestStreaming:
    async def test_the_request_asks_for_a_stream(self) -> None:
        server = FakeServer(ndjson_response(piece("Hi"), FINAL))

        await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

        assert server.sent["stream"] is True
        assert str(server.request.url) == f"{BASE}/api/chat"

    async def test_the_text_arrives_in_pieces_then_the_usage_then_the_finish(self) -> None:
        server = FakeServer(ndjson_response(piece("Hel"), piece("lo"), FINAL))

        got = await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [
            TextDelta(text="Hel"),
            TextDelta(text="lo"),
            UsageEvent(usage=Usage(input_tokens=12, output_tokens=3)),
            FinishEvent(reason=FinishReason.STOP),
        ]

    async def test_lines_without_text_are_skipped(self) -> None:
        server = FakeServer(ndjson_response(piece(""), piece("Hi"), piece(""), FINAL))

        got = await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

        assert [event for event in got if isinstance(event, TextDelta)] == [TextDelta(text="Hi")]

    async def test_text_in_the_last_line_is_not_lost(self) -> None:
        last = piece("!", done=True, done_reason="stop")
        server = FakeServer(ndjson_response(piece("Hi"), last))

        got = await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [
            TextDelta(text="Hi"),
            TextDelta(text="!"),
            FinishEvent(reason=FinishReason.STOP),
        ]

    async def test_a_cut_off_answer_says_so(self) -> None:
        cut = piece(done=True, done_reason="length", prompt_eval_count=5, eval_count=50)
        server = FakeServer(ndjson_response(piece("Once upon"), cut))

        got = await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got[-1] == FinishEvent(reason=FinishReason.LENGTH)

    async def test_without_counters_there_is_no_usage_event(self) -> None:
        server = FakeServer(ndjson_response(piece("Hi"), piece(done=True, done_reason="stop")))

        got = await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

        assert got == [TextDelta(text="Hi"), FinishEvent(reason=FinishReason.STOP)]

    async def test_a_stream_that_stops_before_done_is_a_provider_failure(self) -> None:
        server = FakeServer(ndjson_response(piece("Hel"), piece("lo")))

        with pytest.raises(ProviderUnavailableError, match="before the answer was complete"):
            await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_an_error_sent_inside_the_stream_is_raised_after_what_came_before(self) -> None:
        server = FakeServer(ndjson_response(piece("Hel"), {"error": "llama runner crashed"}))

        seen, error = await collect_events(
            OllamaAdapter().chat_stream(server.client, binding_for(), ask())
        )

        assert seen == [TextDelta(text="Hel")]
        assert isinstance(error, ProviderUnavailableError)
        assert "llama runner crashed" in str(error)

    async def test_a_line_that_is_not_json_is_a_provider_failure(self) -> None:
        server = FakeServer(httpx.Response(200, content=b"not json at all\n"))

        with pytest.raises(ProviderUnavailableError, match="not JSON"):
            await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_an_error_status_is_raised_before_any_event(self) -> None:
        server = FakeServer(error_response(404, "model 'nope' not found"))

        with pytest.raises(InvalidRequestError):
            await events(OllamaAdapter().chat_stream(server.client, binding_for(), ask()))

    async def test_stopping_early_closes_the_stream_cleanly(self) -> None:
        server = FakeServer(ndjson_response(piece("a"), piece("b"), piece("c"), FINAL))
        stream = OllamaAdapter().chat_stream(server.client, binding_for(), ask())

        first = await anext(stream)
        await stream.aclose()

        assert first == TextDelta(text="a")


class TestEmbeddings:
    @staticmethod
    def vectors(*rows: list[float], tokens: int | None = None) -> httpx.Response:
        body: dict[str, Any] = {"model": "nomic-embed-text", "embeddings": list(rows)}
        if tokens is not None:
            body["prompt_eval_count"] = tokens
        return json_response(body)

    async def test_the_request_goes_to_the_embed_endpoint_with_the_whole_batch(self) -> None:
        server = FakeServer(self.vectors([0.1, 0.2], [0.3, 0.4]))

        await OllamaAdapter().embed(server.client, binding_for(), ["first", "second"])

        assert str(server.request.url) == f"{BASE}/api/embed"
        assert server.sent["model"] == "llama3.1:8b"
        assert server.sent["input"] == ["first", "second"]

    async def test_an_input_that_does_not_fit_is_an_error_and_not_silently_shortened(self) -> None:
        server = FakeServer(self.vectors([0.1]))

        await OllamaAdapter().embed(server.client, binding_for(), ["text"])

        assert server.sent["truncate"] is False

    async def test_the_window_and_the_size_are_sent_only_when_declared(self) -> None:
        plain = FakeServer(self.vectors([0.1]))
        declared = FakeServer(self.vectors([0.1]))

        await OllamaAdapter().embed(plain.client, binding_for(), ["text"])
        await OllamaAdapter().embed(
            declared.client,
            binding_for(options=BindingOptions(num_ctx=2048, dimensions=256)),
            ["text"],
        )

        assert "options" not in plain.sent
        assert "dimensions" not in plain.sent
        assert declared.sent["options"] == {"num_ctx": 2048}
        assert declared.sent["dimensions"] == 256

    async def test_vectors_and_usage_are_normalized(self) -> None:
        server = FakeServer(self.vectors([0.1, 0.2], [0.3, 0.4], tokens=11))

        batch = await OllamaAdapter().embed(server.client, binding_for(), ["a", "b"])

        assert batch.vectors == ((0.1, 0.2), (0.3, 0.4))
        assert batch.usage == Usage(input_tokens=11)

    async def test_a_server_that_reports_no_counter_gives_no_usage(self) -> None:
        server = FakeServer(self.vectors([0.1]))

        batch = await OllamaAdapter().embed(server.client, binding_for(), ["a"])

        assert batch.usage is None

    @pytest.mark.parametrize(
        "body", [{}, {"embeddings": []}, {"embeddings": "nope"}, {"embeddings": [["x"]]}]
    )
    async def test_an_answer_of_another_shape_is_a_provider_failure(self, body: Any) -> None:
        server = FakeServer(json_response(body))

        with pytest.raises(ProviderUnavailableError, match="expected shape"):
            await OllamaAdapter().embed(server.client, binding_for(), ["a"])

    async def test_an_error_in_the_body_is_raised(self) -> None:
        server = FakeServer(json_response({"error": "model is not an embedding model"}))

        with pytest.raises(ProviderUnavailableError, match="not an embedding model"):
            await OllamaAdapter().embed(server.client, binding_for(), ["a"])
