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
from factotum.conversation.gate import RESPONSE_SCHEMA, SYSTEM_PROMPT, ReferenceGate
from factotum.conversation.testing import FakeChat

OWNER = Owner(kind=OwnerKind.USER, id=uuid4())


class TestWhatItAsks:
    async def test_it_asks_the_fast_model_about_the_message_alone(self) -> None:
        chat = FakeChat()

        await ReferenceGate(chat).decide(OWNER, "what does the warranty not cover?")

        (request,) = chat.classifications
        assert request.role is ModelRole.CHAT_FAST
        assert [m.role for m in request.messages] == [MessageRole.SYSTEM, MessageRole.USER]
        assert request.messages[1].content == "what does the warranty not cover?"
        assert request.messages[0].content == SYSTEM_PROMPT

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

    def test_the_schema_is_the_one_of_a_decision(self) -> None:
        assert RESPONSE_SCHEMA["properties"]["kind"]["$ref"].endswith("ReferenceKind")
        assert RESPONSE_SCHEMA["required"] == ["kind"]

    def test_the_prompt_says_what_each_kind_is_and_shows_them(self) -> None:
        for kind in ReferenceKind:
            assert f'"{kind.value}"' in SYSTEM_PROMPT
        for period in Period:
            assert f'"{period.value}"' in SYSTEM_PROMPT

    def test_the_prompt_tells_a_model_it_has_not_seen_the_conversation(self) -> None:
        assert "not shown the conversation" in SYSTEM_PROMPT


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

    async def test_an_answer_that_is_not_a_decision_is_refused(self) -> None:
        class Confused(FakeChat):
            async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
                return ChatResult(
                    text="{}", parsed={"kind": "maybe"}, finish_reason=FinishReason.STOP
                )

        with pytest.raises(GatewayError, match="not a decision"):
            await ReferenceGate(Confused()).decide(OWNER, "anything")

    async def test_no_answer_at_all_is_refused_too(self) -> None:
        class Silent(FakeChat):
            async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
                return ChatResult(text="", parsed=None, finish_reason=FinishReason.STOP)

        with pytest.raises(GatewayError, match="not a decision"):
            await ReferenceGate(Silent()).decide(OWNER, "anything")


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
