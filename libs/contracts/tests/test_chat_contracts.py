from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from factotum.contracts.chat import (
    ChatEvent,
    ChatMessage,
    ChatRequest,
    ChatResult,
    ErrorEvent,
    FinishEvent,
    FinishReason,
    MessageRole,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.models import ModelRole

event_adapter: TypeAdapter[Any] = TypeAdapter(ChatEvent)


def message(role: MessageRole, content: str = "hello") -> ChatMessage:
    return ChatMessage(role=role, content=content)


def request(**overrides: Any) -> ChatRequest:
    fields: dict[str, Any] = {"messages": (message(MessageRole.USER),)}
    return ChatRequest(**(fields | overrides))


class TestChatRequest:
    def test_a_plain_question_uses_the_main_role(self) -> None:
        chat = request()

        assert chat.role is ModelRole.CHAT_MAIN
        assert chat.stream is False
        assert chat.response_schema is None

    def test_a_conversation_may_start_with_system_messages(self) -> None:
        chat = request(
            messages=(
                message(MessageRole.SYSTEM, "be brief"),
                message(MessageRole.USER, "hi"),
                message(MessageRole.ASSISTANT, "hello"),
                message(MessageRole.USER, "and now?"),
            )
        )

        assert len(chat.messages) == 4

    @pytest.mark.parametrize("last", [MessageRole.ASSISTANT, MessageRole.SYSTEM])
    def test_the_last_message_must_be_the_users(self, last: MessageRole) -> None:
        with pytest.raises(ValidationError, match="last message must be a user message"):
            request(messages=(message(MessageRole.USER), message(last)))

    def test_there_must_be_at_least_one_message(self) -> None:
        with pytest.raises(ValidationError):
            request(messages=())

    def test_a_message_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            message(MessageRole.USER, "")

    @pytest.mark.parametrize("role", [ModelRole.EMBEDDING, ModelRole.STT, ModelRole.WEB_SEARCH])
    def test_only_chat_roles_can_chat(self, role: ModelRole) -> None:
        with pytest.raises(ValidationError, match="not a chat role"):
            request(role=role)

    def test_the_fast_role_is_a_chat_role(self) -> None:
        assert request(role=ModelRole.CHAT_FAST).role is ModelRole.CHAT_FAST

    def test_structured_output_cannot_be_streamed(self) -> None:
        with pytest.raises(ValidationError, match="cannot be streamed"):
            request(stream=True, response_schema={"type": "object"})

    def test_structured_output_and_streaming_are_fine_separately(self) -> None:
        assert request(response_schema={"type": "object"}).response_schema == {"type": "object"}
        assert request(stream=True).stream is True

    @pytest.mark.parametrize(("field", "value"), [("temperature", 3), ("max_output_tokens", 0)])
    def test_limits_are_bounded(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError, match=field):
            request(**{field: value})

    def test_a_request_names_no_model(self) -> None:
        assert "model" not in ChatRequest.model_fields

    def test_a_request_does_not_classify_its_data(self) -> None:
        # Where the data goes is decided by the deployment and the providers the user picked
        # (ADR 0010). A caller that still sends a classification is refused, not ignored: it must
        # not believe that something enforces it.
        with pytest.raises(ValidationError):
            request(data_classification="local_only")

    def test_a_request_round_trips_through_json(self) -> None:
        chat = request(
            messages=(message(MessageRole.SYSTEM, "s"), message(MessageRole.USER, "u")),
            response_schema={"type": "object", "properties": {"a": {"type": "string"}}},
            temperature=0.2,
        )

        assert ChatRequest.model_validate_json(chat.model_dump_json()) == chat


class TestChatResult:
    def test_a_result_carries_text_reason_and_usage(self) -> None:
        result = ChatResult(
            text="hello",
            finish_reason=FinishReason.STOP,
            usage=Usage(input_tokens=10, output_tokens=2),
        )

        assert result.parsed is None
        assert result.usage is not None
        assert result.usage.output_tokens == 2

    def test_parsed_can_be_any_json_value(self) -> None:
        for value in ({"a": 1}, [1, 2], "text", 3, None):
            result = ChatResult(text="x", parsed=value, finish_reason=FinishReason.STOP)
            assert ChatResult.model_validate_json(result.model_dump_json()).parsed == value

    def test_token_counts_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            Usage(input_tokens=-1)


class TestChatEvents:
    @pytest.mark.parametrize(
        "event",
        [
            TextDelta(text="hel"),
            UsageEvent(usage=Usage(input_tokens=1, output_tokens=2)),
            FinishEvent(reason=FinishReason.LENGTH),
            ErrorEvent(code="provider_unavailable", message="overloaded"),
        ],
        ids=lambda e: e.type,
    )
    def test_an_event_round_trips_through_the_discriminated_union(self, event: Any) -> None:
        restored = event_adapter.validate_json(event.model_dump_json())

        assert restored == event
        assert type(restored) is type(event)

    @pytest.mark.parametrize("chunk", [" world", "Hello ", " and ", "\n", "  indented"])
    def test_a_chunk_of_text_keeps_the_spaces_at_its_edges(self, chunk: str) -> None:
        event = TextDelta(text=chunk)

        assert event.text == chunk
        assert event_adapter.validate_json(event.model_dump_json()) == event

    def test_the_chunks_of_an_answer_put_back_together_are_the_answer(self) -> None:
        chunks = ["Hold", " the", " reset", " button", " for", " ten", " seconds", "."]

        assert (
            "".join(TextDelta(text=c).text for c in chunks)
            == "Hold the reset button for ten seconds."
        )

    def test_the_type_selects_the_event(self) -> None:
        assert isinstance(
            event_adapter.validate_python({"type": "text_delta", "text": "a"}), TextDelta
        )

    def test_an_unknown_event_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            event_adapter.validate_python({"type": "thinking", "text": "hmm"})
