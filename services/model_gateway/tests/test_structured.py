import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.contracts.chat import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    FinishReason,
    MessageRole,
    Usage,
)
from factotum.contracts.errors import (
    ContentFilteredError,
    InvalidSchemaError,
    InvalidStructuredOutputError,
    ProviderUnavailableError,
)
from factotum.model_gateway.structured import (
    extract_json,
    repair_messages,
    run_structured,
    strict_compatible,
    validator_for,
    with_json_instructions,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "days": {"type": "integer", "minimum": 1}},
    "required": ["city"],
    "additionalProperties": False,
}


def user(text: str = "weather?") -> ChatMessage:
    return ChatMessage(role=MessageRole.USER, content=text)


def request(schema: dict[str, Any] | None = SCHEMA, *messages: ChatMessage) -> ChatRequest:
    return ChatRequest(messages=messages or (user(),), response_schema=schema)


def answer(
    text: str, *, tokens: tuple[int, int] = (10, 5), finish: FinishReason = FinishReason.STOP
) -> ChatResult:
    return ChatResult(
        text=text,
        finish_reason=finish,
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


class Scripted:
    """A chat call that answers as told, one answer per call, and remembers what it was sent."""

    def __init__(self, *answers: ChatResult | Exception) -> None:
        self._answers = list(answers)
        self.sent: list[ChatRequest] = []

    async def __call__(self, sent: ChatRequest) -> ChatResult:
        self.sent.append(sent)
        result = self._answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestExtractJson:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('{"a": 1}', {"a": 1}),
            ('  {"a": 1}  \n', {"a": 1}),
            ('```json\n{"a": 1}\n```', {"a": 1}),
            ('```\n{"a": 1}\n```', {"a": 1}),
            ('Here is the JSON you asked for: {"a": 1} Hope it helps!', {"a": 1}),
            ('Sure.\n```json\n{"a": [1, 2]}\n```\nAnything else?', {"a": [1, 2]}),
            ("[1, 2, 3]", [1, 2, 3]),
            ('the {placeholder} is filled: {"a": 2}', {"a": 2}),
            ("42", 42),
        ],
    )
    def test_the_value_is_found_around_the_prose(self, text: str, expected: Any) -> None:
        assert extract_json(text) == expected

    @pytest.mark.parametrize("text", ["", "no json here", "{broken", '{"a": '])
    def test_an_answer_without_json_is_an_error(self, text: str) -> None:
        with pytest.raises(ValueError, match="no JSON value"):
            extract_json(text)


class TestStrictCompatible:
    def test_an_object_that_forbids_extras_and_requires_everything_is_compatible(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        }

        assert strict_compatible(schema)

    @pytest.mark.parametrize(
        "schema",
        [
            SCHEMA,  # `days` is optional and has `minimum`
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {
                "type": "object",
                "properties": {"a": {"type": "string", "pattern": "^x"}},
                "required": ["a"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"a": {"$ref": "#/$defs/x"}},
                "required": ["a"],
                "additionalProperties": False,
            },
            {"type": "string", "minLength": 2},
            {"type": "array", "items": {"type": "string", "minLength": 1}},
            "not a schema",
            None,
        ],
        ids=[
            "optional-and-minimum",
            "extras-allowed",
            "pattern",
            "ref",
            "minLength",
            "loose-items",
            "not-a-schema",
            "none",
        ],
    )
    def test_anything_else_is_not(self, schema: Any) -> None:
        assert not strict_compatible(schema)

    def test_nested_objects_are_checked_too(self) -> None:
        loose_inner = {
            "type": "object",
            "properties": {
                "inner": {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}
            },
            "required": ["inner"],
            "additionalProperties": False,
        }

        assert not strict_compatible(loose_inner)

    def test_the_branches_of_any_of_are_checked(self) -> None:
        good = {"anyOf": [{"type": "string"}, {"type": "null"}]}
        bad = {"anyOf": [{"type": "string"}, {"type": "string", "minLength": 1}]}

        assert strict_compatible(good)
        assert not strict_compatible(bad)


class TestSchemaValidation:
    def test_a_valid_schema_gives_a_validator(self) -> None:
        assert validator_for(SCHEMA).is_valid({"city": "Rome"})

    def test_an_invalid_schema_is_reported_as_such(self) -> None:
        with pytest.raises(InvalidSchemaError, match="not a valid JSON Schema"):
            validator_for({"type": "not-a-type"})


class TestPromptBuilding:
    def test_instructions_are_appended_to_an_existing_system_message(self) -> None:
        messages = (ChatMessage(role=MessageRole.SYSTEM, content="Be brief."), user())

        built = with_json_instructions(messages, SCHEMA)

        assert len(built) == 2
        assert built[0].content.startswith("Be brief.\n\nReply with a single JSON value")
        assert '"city"' in built[0].content

    def test_a_system_message_is_added_when_there_is_none(self) -> None:
        built = with_json_instructions((user(),), SCHEMA)

        assert [message.role for message in built] == [MessageRole.SYSTEM, MessageRole.USER]

    def test_the_original_messages_are_not_modified(self) -> None:
        messages = (user(),)

        with_json_instructions(messages, SCHEMA)

        assert messages == (user(),)

    def test_a_repair_shows_the_bad_answer_and_the_problems(self) -> None:
        built = repair_messages((user(),), '{"days": 0}', ["city: is required", "days: too small"])

        assert [message.role for message in built] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.USER,
        ]
        assert built[1].content == '{"days": 0}'
        assert "- city: is required" in built[2].content
        assert "- days: too small" in built[2].content

    def test_an_empty_bad_answer_is_still_a_valid_message(self) -> None:
        assert repair_messages((user(),), "", ["x"])[1].content == "(empty)"


class TestRunStructured:
    async def test_a_valid_native_answer_is_parsed_and_returned(self) -> None:
        chat = Scripted(answer('{"city": "Rome", "days": 3}'))

        result = await run_structured(chat, request(), native=True)

        assert result.parsed == {"city": "Rome", "days": 3}
        assert result.text == '{"city": "Rome", "days": 3}'
        assert len(chat.sent) == 1

    async def test_native_mode_hands_the_provider_the_schema_and_the_plain_conversation(
        self,
    ) -> None:
        chat = Scripted(answer('{"city": "Rome"}'))

        await run_structured(chat, request(), native=True)

        assert chat.sent[0].response_schema == SCHEMA
        assert chat.sent[0].messages == (user(),)

    async def test_emulated_mode_puts_the_schema_in_the_prompt_and_not_in_the_request(self) -> None:
        chat = Scripted(answer('{"city": "Rome"}'))

        await run_structured(chat, request(), native=False)

        sent = chat.sent[0]
        assert sent.response_schema is None
        assert sent.messages[0].role is MessageRole.SYSTEM
        assert "JSON Schema" in sent.messages[0].content

    async def test_an_answer_wrapped_in_prose_and_fences_is_accepted(self) -> None:
        chat = Scripted(answer('Sure!\n```json\n{"city": "Rome"}\n```'))

        assert (await run_structured(chat, request(), native=False)).parsed == {"city": "Rome"}

    async def test_an_invalid_answer_is_sent_back_with_the_problems(self) -> None:
        chat = Scripted(answer('{"days": 0}'), answer('{"city": "Rome", "days": 2}'))

        result = await run_structured(chat, request(), native=True)

        assert result.parsed == {"city": "Rome", "days": 2}
        retry = chat.sent[1].messages
        assert retry[-2].role is MessageRole.ASSISTANT
        assert retry[-2].content == '{"days": 0}'
        assert "city" in retry[-1].content
        assert "required" in retry[-1].content

    async def test_the_repair_starts_again_from_the_original_conversation(self) -> None:
        chat = Scripted(answer("nonsense"), answer("still nonsense"), answer('{"city": "Rome"}'))

        await run_structured(chat, request(), native=True)

        # Not answer-after-answer-after-answer: each retry shows only the latest failure.
        assert len(chat.sent[2].messages) == 3

    async def test_an_answer_that_is_not_json_is_repaired_too(self) -> None:
        chat = Scripted(answer("I cannot do that"), answer('{"city": "Rome"}'))

        result = await run_structured(chat, request(), native=False)

        assert result.parsed == {"city": "Rome"}
        assert "not valid JSON" in chat.sent[1].messages[-1].content

    async def test_it_gives_up_after_the_repairs_and_says_why(self) -> None:
        chat = Scripted(answer("{}"), answer("{}"), answer("{}"))

        with pytest.raises(InvalidStructuredOutputError, match="after 3 attempts") as caught:
            await run_structured(chat, request(), native=True, max_repairs=2)

        assert caught.value.details["attempts"] == 3
        assert any("city" in problem for problem in caught.value.details["problems"])
        assert len(chat.sent) == 3

    async def test_no_repairs_means_one_attempt(self) -> None:
        chat = Scripted(answer("{}"))

        with pytest.raises(InvalidStructuredOutputError, match="after 1 attempts"):
            await run_structured(chat, request(), native=True, max_repairs=0)

    async def test_an_answer_cut_off_by_the_limit_is_not_retried(self) -> None:
        chat = Scripted(
            answer('{"city": "Ro', finish=FinishReason.LENGTH), answer('{"city": "Rome"}')
        )

        with pytest.raises(InvalidStructuredOutputError, match="cut off"):
            await run_structured(chat, request(), native=True)

        assert len(chat.sent) == 1

    async def test_a_refusal_is_reported_as_such_and_not_retried(self) -> None:
        chat = Scripted(answer("", finish=FinishReason.CONTENT_FILTER))

        with pytest.raises(ContentFilteredError):
            await run_structured(chat, request(), native=True)

        assert len(chat.sent) == 1

    async def test_a_refusal_with_text_is_not_mistaken_for_an_invalid_answer(self) -> None:
        chat = Scripted(answer("I can't help with that.", finish=FinishReason.CONTENT_FILTER))

        with pytest.raises(ContentFilteredError):
            await run_structured(chat, request(), native=True)

        assert len(chat.sent) == 1

    async def test_token_usage_is_added_up_across_attempts(self) -> None:
        chat = Scripted(answer("{}", tokens=(10, 4)), answer('{"city": "Rome"}', tokens=(30, 6)))

        result = await run_structured(chat, request(), native=True)

        assert result.usage == Usage(input_tokens=40, output_tokens=10)

    async def test_usage_reported_by_only_some_attempts_still_counts(self) -> None:
        silent = ChatResult(text='{"city": "Rome"}', finish_reason=FinishReason.STOP)
        chat = Scripted(answer("{}", tokens=(10, 4)), silent)

        result = await run_structured(chat, request(), native=True)

        assert result.usage == Usage(input_tokens=10, output_tokens=4)

    async def test_usage_reported_only_by_the_last_attempt_counts_too(self) -> None:
        silent = ChatResult(text="{}", finish_reason=FinishReason.STOP)
        chat = Scripted(silent, answer('{"city": "Rome"}', tokens=(30, 6)))

        result = await run_structured(chat, request(), native=True)

        assert result.usage == Usage(input_tokens=30, output_tokens=6)

    async def test_missing_usage_is_tolerated(self) -> None:
        no_usage = ChatResult(text='{"city": "Rome"}', finish_reason=FinishReason.STOP)

        result = await run_structured(Scripted(no_usage), request(), native=True)

        assert result.usage is None

    async def test_provider_errors_pass_through_untouched(self) -> None:
        chat = Scripted(ProviderUnavailableError("down"))

        with pytest.raises(ProviderUnavailableError):
            await run_structured(chat, request(), native=True)

    async def test_an_invalid_schema_is_refused_before_anything_is_sent(self) -> None:
        chat = Scripted(answer("{}"))

        with pytest.raises(InvalidSchemaError):
            await run_structured(chat, request({"type": "bogus"}), native=True)

        assert chat.sent == []

    async def test_a_request_without_a_schema_is_a_programming_error(self) -> None:
        with pytest.raises(InvalidSchemaError, match="needs a response schema"):
            await run_structured(Scripted(), request(None), native=True)

    async def test_the_reported_problems_are_limited(self) -> None:
        schema = {
            "type": "object",
            "properties": {f"p{i}": {"type": "string"} for i in range(20)},
            "required": [f"p{i}" for i in range(20)],
        }
        chat = Scripted(answer("{}"))

        with pytest.raises(InvalidStructuredOutputError) as caught:
            await run_structured(chat, request(schema), native=True, max_repairs=0)

        assert len(caught.value.details["problems"]) == 5


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8), st.integers() | st.text(max_size=8), max_size=5
    )
)
def test_extract_json_recovers_any_object_from_its_serialization(payload: dict[str, Any]) -> None:
    assert extract_json(json.dumps(payload)) == payload
    assert extract_json(f"Here you go:\n```json\n{json.dumps(payload)}\n```") == payload
