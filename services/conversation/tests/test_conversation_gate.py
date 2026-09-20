import asyncio
from uuid import uuid4

import pytest

from factotum.contracts.chat import ChatRequest, ChatResult, FinishReason, MessageRole
from factotum.contracts.conversation import Period, Reference, ReferenceKind
from factotum.contracts.errors import (
    GatewayError,
    GatewayUnavailableError,
    ProviderTimeoutError,
)
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.models import ModelRole
from factotum.conversation.gate import (
    EXAMPLES,
    MAX_CHARS,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    Reading,
    ReferenceGate,
    shortened,
)
from factotum.conversation.gate_cases import GATE_CASES
from factotum.conversation.testing import FakeChat

OWNER = Owner(kind=OwnerKind.USER, id=uuid4())


class Saying(FakeChat):
    """A model that answers whatever it is asked with the same thing."""

    def __init__(self, parsed: object) -> None:
        super().__init__()
        self.parsed = parsed

    async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
        return ChatResult(text="", parsed=self.parsed, finish_reason=FinishReason.STOP)


class TestWhatItAsks:
    async def test_it_asks_the_fast_model(self) -> None:
        chat = FakeChat()

        await ReferenceGate(chat).decide(OWNER, "what does the warranty not cover?")

        (request,) = chat.classifications
        assert request.role is ModelRole.CHAT_FAST
        assert request.messages[0].role is MessageRole.SYSTEM
        assert request.messages[0].content == SYSTEM_PROMPT

    async def test_the_message_comes_last_and_is_all_of_the_conversation_it_gets(self) -> None:
        chat = FakeChat()
        gate = ReferenceGate(chat)

        await gate.decide(OWNER, "what does the warranty not cover?")
        await gate.decide(OWNER, "and in English?")

        first, second = chat.classifications
        assert first.messages[-1].role is MessageRole.USER
        assert first.messages[-1].content == "what does the warranty not cover?"
        assert second.messages[-1].content == "and in English?"
        # Everything before it is the instructions and the examples, the same for every message:
        # nothing that was said before can reach the model, or steer it.
        assert first.messages[:-1] == second.messages[:-1]

    async def test_the_examples_are_turns_of_the_conversation_the_model_is_in(self) -> None:
        chat = FakeChat()

        await ReferenceGate(chat).decide(OWNER, "hello")

        (request,) = chat.classifications
        shown = request.messages[1:-1]
        assert [m.role for m in shown] == [MessageRole.USER, MessageRole.ASSISTANT] * len(EXAMPLES)
        assert [m.content for m in shown[0::2]] == [e.message for e in EXAMPLES]
        assert [m.content for m in shown[1::2]] == [e.answer() for e in EXAMPLES]

    async def test_it_asks_for_a_decision_and_nothing_else_and_for_it_to_be_the_same_every_time(
        self,
    ) -> None:
        chat = FakeChat()

        await ReferenceGate(chat).decide(OWNER, "hello")

        (request,) = chat.classifications
        assert request.response_schema == RESPONSE_SCHEMA
        assert request.temperature == 0
        assert request.max_output_tokens is not None
        assert request.max_output_tokens <= 200

    async def test_it_acts_for_the_owner_of_the_message(self) -> None:
        chat = FakeChat()

        await ReferenceGate(chat).decide(OWNER, "hello")

        assert chat.requests[0][0] == OWNER

    async def test_a_long_message_is_read_by_its_two_ends(self) -> None:
        chat = FakeChat()
        message = "As you said yesterday, " + "blah " * 2000 + "so what was that recipe?"

        await ReferenceGate(chat).decide(OWNER, message)

        (request,) = chat.classifications
        read = request.messages[-1].content
        assert read == shortened(message)
        assert read.startswith("As you said yesterday")
        assert read.endswith("so what was that recipe?")

    def test_the_answer_asked_for_says_why_before_it_says_which(self) -> None:
        assert list(RESPONSE_SCHEMA["properties"]) == ["reason", "kind", "topic", "when"]
        assert RESPONSE_SCHEMA["required"] == ["reason", "kind"]
        assert RESPONSE_SCHEMA["properties"]["reason"]["maxLength"] <= 200

    def test_the_prompt_says_what_each_kind_and_period_is(self) -> None:
        for kind in ReferenceKind:
            assert f'"{kind.value}"' in SYSTEM_PROMPT
        for period in Period:
            assert f'"{period.value}"' in SYSTEM_PROMPT

    def test_the_prompt_tells_a_model_it_has_not_seen_the_conversation(self) -> None:
        assert "not shown the conversation" in SYSTEM_PROMPT


class TestShorteningAMessage:
    def test_a_message_that_is_short_enough_is_read_as_it_is(self) -> None:
        assert shortened("and in English?") == "and in English?"
        assert shortened("a" * MAX_CHARS) == "a" * MAX_CHARS

    def test_a_longer_one_is_cut_in_the_middle_and_keeps_both_ends(self) -> None:
        message = "BEGIN " + "x" * 5000 + " END"

        read = shortened(message)

        assert len(read) <= MAX_CHARS + 3
        assert read.startswith("BEGIN ")
        assert read.endswith(" END")
        assert " … " in read


class TestTheExamples:
    def test_each_is_an_answer_the_schema_accepts_and_reads_as_it_was_meant(self) -> None:
        for example in EXAMPLES:
            assert Reading.model_validate_json(example.answer()).reference() == example.reference

    def test_the_reason_comes_before_the_kind_in_what_is_shown(self) -> None:
        for example in EXAMPLES:
            assert example.answer().startswith('{"reason": ')
            assert example.answer().index('"kind"') > example.answer().index('"reason"')

    def test_every_kind_is_shown_a_good_few_times(self) -> None:
        for kind in ReferenceKind:
            assert sum(e.reference.kind is kind for e in EXAMPLES) >= 5

    def test_most_are_of_what_most_messages_are(self) -> None:
        # What is shown sets what a model expects: a message that stands alone is what is said most.
        by_kind = {k: sum(e.reference.kind is k for e in EXAMPLES) for k in ReferenceKind}

        assert by_kind[ReferenceKind.NONE] == max(by_kind.values())

    def test_a_topic_and_a_period_are_shown_only_where_they_mean_something(self) -> None:
        for example in EXAMPLES:
            if example.reference.kind is not ReferenceKind.EARLIER:
                assert example.reference.topic is None
                assert example.reference.when is None

    def test_an_earlier_exchange_is_shown_with_a_period_a_few_times_and_without_one_a_few(
        self,
    ) -> None:
        earlier = [e.reference for e in EXAMPLES if e.reference.kind is ReferenceKind.EARLIER]

        assert sum(r.when is not None for r in earlier) >= 2
        assert sum(r.when is None for r in earlier) >= 2

    def test_no_message_is_shown_twice(self) -> None:
        messages = [e.message.casefold() for e in EXAMPLES]

        assert len(messages) == len(set(messages))

    def test_a_reason_is_a_few_words(self) -> None:
        assert all(len(e.reason.split()) <= 10 for e in EXAMPLES)

    def test_none_of_them_is_a_message_the_gate_is_measured_with(self) -> None:
        # A model shown the answer to a question is not being tested by asking it.
        shown = {e.message.casefold() for e in EXAMPLES}

        assert not shown & {case.message.casefold() for case in GATE_CASES}


class TestTheMessagesItIsMeasuredWith:
    def test_every_kind_is_there_in_number_and_none_is_the_most(self) -> None:
        by_kind = {k: sum(c.kind is k for c in GATE_CASES) for k in ReferenceKind}

        assert min(by_kind.values()) >= 20
        assert by_kind[ReferenceKind.NONE] == max(by_kind.values())

    def test_no_message_is_there_twice(self) -> None:
        messages = [c.message.casefold() for c in GATE_CASES]

        assert len(messages) == len(set(messages))

    def test_a_period_or_a_topic_is_expected_only_of_an_earlier_reference(self) -> None:
        for case in GATE_CASES:
            if case.kind is not ReferenceKind.EARLIER:
                assert case.when is None
                assert case.topic == ()


class TestWhatItAnswers:
    @pytest.mark.parametrize(
        "decision",
        [
            Reference(kind=ReferenceKind.NONE),
            Reference(kind=ReferenceKind.CONTINUATION),
            Reference(kind=ReferenceKind.EARLIER, topic="router", when=Period.YESTERDAY),
            Reference(kind=ReferenceKind.EARLIER, when=Period.TODAY),
        ],
    )
    async def test_it_is_what_the_model_decided(self, decision: Reference) -> None:
        chat = FakeChat()
        chat.decisions.append(decision)

        assert await ReferenceGate(chat).decide(OWNER, "anything") == decision

    @pytest.mark.parametrize("kind", ["none", "continuation"])
    async def test_a_topic_and_a_period_mean_nothing_for_what_is_not_an_earlier_reference(
        self, kind: str
    ) -> None:
        chat = Saying({"reason": "x", "kind": kind, "topic": "router", "when": "today"})

        decided = await ReferenceGate(chat).decide(OWNER, "anything")

        assert decided == Reference(kind=ReferenceKind(kind))

    async def test_a_blank_topic_is_no_topic(self) -> None:
        chat = Saying({"reason": "x", "kind": "earlier", "topic": "  ", "when": "last_week"})

        decided = await ReferenceGate(chat).decide(OWNER, "anything")

        assert decided == Reference(kind=ReferenceKind.EARLIER, when=Period.LAST_WEEK)

    @pytest.mark.parametrize(
        "parsed",
        [
            {"reason": "x", "kind": "maybe"},
            {"kind": "none"},  # no reason: the model was not answering what it was asked
            {"reason": "x", "kind": "earlier", "when": "a long time ago"},
            {"reason": "x" * 500, "kind": "none"},
            {"reason": "x", "kind": "none", "certainty": 0.9},
            [],
            None,
        ],
    )
    async def test_an_answer_that_is_not_a_decision_is_refused(self, parsed: object) -> None:
        with pytest.raises(GatewayError, match="not a decision"):
            await ReferenceGate(Saying(parsed)).decide(OWNER, "anything")


class TestWhenItCannotDecide:
    async def test_what_the_gateway_says_is_raised_as_it_is(self) -> None:
        chat = FakeChat()
        chat.decisions.append(GatewayUnavailableError("down"))

        with pytest.raises(GatewayUnavailableError):
            await ReferenceGate(chat).decide(OWNER, "anything")

    async def test_a_model_that_is_too_slow_is_a_timeout(self) -> None:
        class Slow(FakeChat):
            async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
                await asyncio.sleep(5)
                raise AssertionError("it should have been given up on")

        with pytest.raises(ProviderTimeoutError, match="in time"):
            await ReferenceGate(Slow(), timeout_s=0.05).decide(OWNER, "anything")
