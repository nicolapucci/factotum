import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import timedelta
from uuid import uuid4

import pytest

from factotum.contracts.chat import (
    ChatRequest,
    EventUnion,
    FinishEvent,
    FinishReason,
    MessageRole,
)
from factotum.contracts.conversation import (
    Cited,
    Delta,
    Failed,
    Finished,
    Period,
    Phase,
    Reference,
    ReferenceKind,
    Started,
    Status,
    TurnEvent,
    TurnRequest,
    TurnWarning,
    WarningCode,
)
from factotum.contracts.errors import (
    ChatNotConfiguredError,
    ContextTooLongError,
    ConversationNotFoundError,
    EmbeddingNotConfiguredError,
    GatewayUnavailableError,
    KnowledgeUnavailableError,
)
from factotum.contracts.identity import Owner, Principal
from factotum.contracts.ids import ConversationId, DeviceId
from factotum.contracts.models import ModelRole
from factotum.contracts.testing import make_tenant_id, make_user_id, make_user_principal
from factotum.conversation.store import InMemoryStore
from factotum.conversation.testing import FakeChat, Harness, passage
from factotum.conversation.turn import TurnSettings

TENANT = make_tenant_id()
USER = make_user_id()


def alice(device: DeviceId | None = None) -> Principal:
    return make_user_principal(tenant_id=TENANT, user_id=USER, device_id=device)


def owner_of(principal: Principal) -> Owner:
    return Owner.of_subject(principal.acts_as)


def kinds(events: Sequence[TurnEvent]) -> list[str]:
    return [event.type for event in events]


def said(events: Sequence[TurnEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, Delta))


def finished(events: Sequence[TurnEvent]) -> Finished:
    [last] = [e for e in events if isinstance(e, Finished)]
    return last


def warnings(events: Sequence[TurnEvent]) -> list[WarningCode]:
    return [e.code for e in events if isinstance(e, TurnWarning)]


def phases(events: Sequence[TurnEvent]) -> list[Phase]:
    return [e.phase for e in events if isinstance(e, Status)]


def prompt_of(chat: FakeChat, which: int = -1) -> str:
    """Everything the model was given in one of its answers, instructions apart."""
    return "\n".join(m.content for m in chat.streams[which].messages if m.role is MessageRole.USER)


async def saved(harness: Harness, principal: Principal | None = None) -> list[str]:
    principal = principal or alice()
    owner = owner_of(principal)
    found = await harness.store.list_conversations(principal.tenant_id, owner, limit=50, offset=0)
    questions: list[str] = []
    for conversation in found:
        _, exchanges = await harness.store.get_conversation(
            principal.tenant_id, owner, conversation.id
        )
        questions += [e.question for e in exchanges]
    return sorted(questions)


class TestATurnWithNoHistory:
    async def test_the_events_come_in_order_and_nothing_about_the_past_is_asked(self) -> None:
        harness = Harness()
        harness.documents.passages = [passage("Hold the button.")]
        harness.chat.answers.append(["Hold it ", "for ten seconds [1]."])

        events = await harness.turn(alice(), "how do I reset the router?")

        assert kinds(events) == [
            "started", "status", "status", "delta", "delta", "citations", "finished"
        ]  # fmt: skip
        assert phases(events) == [Phase.SEARCHING, Phase.ANSWERING]
        assert said(events) == "Hold it for ten seconds [1]."
        assert harness.chat.classifications == []  # somebody with no past is not asked about it

    async def test_the_model_is_given_the_message_and_the_passages_and_nothing_else(self) -> None:
        harness = Harness()
        harness.documents.passages = [passage("Hold the button.", title="Router manual")]

        await harness.turn(alice(), "how do I reset the router?")

        request = harness.chat.streams[0]
        assert request.role is ModelRole.CHAT_MAIN
        assert [m.role for m in request.messages] == [MessageRole.SYSTEM, MessageRole.USER]
        assert "[1] Router manual, page 12, Router > Reset\nHold the button." in prompt_of(
            harness.chat
        )
        assert prompt_of(harness.chat).endswith("Message: how do I reset the router?")

    async def test_the_documents_are_searched_for_the_message_as_it_was_written(self) -> None:
        harness = Harness(settings=TurnSettings(document_passages=4))

        await harness.turn(alice(), "how do I reset the router?")

        assert harness.documents.queries == [(owner_of(alice()), "how do I reset the router?", 4)]

    async def test_without_passages_the_message_is_sent_as_it_is(self) -> None:
        harness = Harness()

        await harness.turn(alice(), "what is the capital of Australia?")

        assert prompt_of(harness.chat) == "what is the capital of Australia?"

    async def test_the_length_of_the_answer_and_the_usage_come_from_the_model(self) -> None:
        harness = Harness()
        harness.chat.max_output_tokens = 300

        events = await harness.turn(alice(), "hello")

        assert harness.chat.streams[0].max_output_tokens == 300
        assert finished(events).usage == harness.chat.usage

    async def test_the_exchange_is_saved_when_the_answer_is_complete(self) -> None:
        harness = Harness()
        harness.chat.answers.append(["Ten ", "seconds."])

        events = await harness.turn(alice(), "how long do I hold the button?")

        [started] = [e for e in events if isinstance(e, Started)]
        found, exchanges = await harness.store.get_conversation(
            TENANT, owner_of(alice()), started.conversation_id
        )
        [exchange] = exchanges
        assert (exchange.question, exchange.answer) == (
            "how long do I hold the button?",
            "Ten seconds.",
        )
        assert exchange.id == started.exchange_id
        assert found.exchanges == 1

    async def test_the_passages_an_answer_cites_are_told_and_kept(self) -> None:
        harness = Harness()
        first, second = passage("One.", title="A", page=3), passage("Two.", title="B", page=None)
        harness.documents.passages = [first, second]
        harness.chat.answers.append(["A says so [1] and B [2], not [7]."])

        events = await harness.turn(alice(), "what do they say?")

        [cited] = [e for e in events if isinstance(e, Cited)]
        assert [(c.number, c.document_title, c.page) for c in cited.citations] == [
            (1, "A", 3),
            (2, "B", None),
        ]
        [started] = [e for e in events if isinstance(e, Started)]
        _, [exchange] = await harness.store.get_conversation(
            TENANT, owner_of(alice()), started.conversation_id
        )
        assert exchange.citations == cited.citations

    @pytest.mark.parametrize("with_passages", [True, False])
    async def test_a_turn_with_passages_has_somebody_elses_words_in_it_and_says_so(
        self, with_passages: bool
    ) -> None:
        harness = Harness()
        if with_passages:
            harness.documents.passages = [passage()]

        events = await harness.turn(alice(), "how do I reset the router?")

        assert finished(events).untrusted_content is with_passages
        assert finished(events).passages_used == (1 if with_passages else 0)
        [started] = [e for e in events if isinstance(e, Started)]
        _, [exchange] = await harness.store.get_conversation(
            TENANT, owner_of(alice()), started.conversation_id
        )
        assert exchange.untrusted is with_passages


class TestAMessageThatStandsAlone:
    async def test_it_carries_nothing_of_what_was_said_before(self) -> None:
        harness = Harness()
        await harness.turn(alice(), "what is the wifi password of the guest network?")
        harness.chat.answers.append(["Paris."])

        events = await harness.turn(alice(), "what is the capital of France?")

        assert prompt_of(harness.chat) == "what is the capital of France?"
        assert finished(events).reference is ReferenceKind.NONE
        assert finished(events).exchanges_used == 0

    async def test_the_small_model_is_asked_about_the_message_alone(self) -> None:
        harness = Harness()
        await harness.turn(alice(), "first")

        events = await harness.turn(alice(), "what is the capital of France?")

        [request] = harness.chat.classifications
        sent = [m.content for m in request.messages]
        assert sent[-1] == "what is the capital of France?"
        assert "first" not in sent  # what was said before stays out of it
        assert "An answer." not in sent
        assert phases(events)[0] is Phase.UNDERSTANDING
        assert Phase.RECALLING not in phases(events)


class TestAContinuation:
    async def turn_after_one(self, harness: Harness, question: str = "how do I reset it?") -> None:
        harness.chat.answers.append(["Hold the button."])
        await harness.turn(alice(), question)
        harness.chat.decisions.append(Reference(kind=ReferenceKind.CONTINUATION))

    async def test_the_exchange_just_before_is_what_the_message_carries_on(self) -> None:
        harness = Harness()
        await self.turn_after_one(harness)
        harness.chat.answers.append(["Tieni premuto il pulsante."])

        events = await harness.turn(alice(), "and in Italian?")

        given = prompt_of(harness.chat)
        assert "=== EARLIER EXCHANGES nonce123 ===" in given
        assert "You: how do I reset it?\nAssistant: Hold the button." in given
        assert given.endswith("Message: and in Italian?")
        assert finished(events).reference is ReferenceKind.CONTINUATION
        assert finished(events).exchanges_used == 1
        assert Phase.RECALLING in phases(events)

    async def test_the_documents_are_searched_for_the_message_together_with_that_question(
        self,
    ) -> None:
        harness = Harness()
        await self.turn_after_one(harness, "how do I reset the router?")

        await harness.turn(alice(), "and the firmware?")

        assert harness.documents.queries[-1][1] == "how do I reset the router?\nand the firmware?"

    async def test_only_the_last_few_exchanges_come_back_and_all_of_them_are_in_order(self) -> None:
        harness = Harness(settings=TurnSettings(recent_exchanges=3))
        for n in range(5):
            harness.chat.answers.append([f"answer {n}"])
            await harness.turn(alice(), f"question {n}")
            harness.clock.advance(minutes=1)
        harness.chat.decisions.append(Reference(kind=ReferenceKind.CONTINUATION))

        events = await harness.turn(alice(), "and after that?")

        given = prompt_of(harness.chat)
        assert finished(events).exchanges_used == 3
        for n in (0, 1):
            assert f"question {n}" not in given
        assert given.index("question 2") < given.index("question 3") < given.index("question 4")

    async def test_a_conversation_that_has_just_begun_has_nothing_to_carry_on(self) -> None:
        harness = Harness()
        await harness.turn(alice(), "long ago")
        harness.clock.advance(hours=2)  # the next message begins another conversation
        harness.chat.decisions.append(Reference(kind=ReferenceKind.CONTINUATION))

        events = await harness.turn(alice(), "and in English?")

        assert finished(events).exchanges_used == 0
        assert "EARLIER EXCHANGES" not in prompt_of(harness.chat)


class TestSomethingSaidEarlier:
    async def a_day_after(self, harness: Harness) -> None:
        harness.chat.answers.append(["Hold the reset button for ten seconds."])
        await harness.turn(alice(), "how do I reset the router?")
        harness.chat.answers.append(["Carbonara has eggs."])
        await harness.turn(alice(), "what is in a carbonara?")
        harness.clock.advance(days=1)

    async def test_the_exchange_that_was_meant_is_found_and_quoted(self) -> None:
        harness = Harness()
        await self.a_day_after(harness)
        harness.chat.decisions.append(
            Reference(kind=ReferenceKind.EARLIER, topic="router", when=Period.YESTERDAY)
        )

        events = await harness.turn(alice(), "what did you tell me yesterday about the router?")

        given = prompt_of(harness.chat)
        assert "Hold the reset button for ten seconds." in given
        assert "carbonara" not in given
        assert finished(events).reference is ReferenceKind.EARLIER
        assert finished(events).exchanges_used == 1
        assert Phase.RECALLING in phases(events)

    async def test_a_message_that_names_neither_a_topic_nor_a_time_is_looked_up_by_what_it_says(
        self,
    ) -> None:
        harness = Harness()
        await self.a_day_after(harness)
        await harness.history.embed_pending()
        harness.chat.decisions.append(Reference(kind=ReferenceKind.EARLIER))

        events = await harness.turn(alice(), "what was that reset button of the router?")

        given = prompt_of(harness.chat)
        assert "Hold the reset button for ten seconds." in given
        assert "carbonara" not in given
        assert finished(events).exchanges_used == 1

    async def test_a_period_the_model_got_wrong_does_not_hide_what_was_meant(self) -> None:
        harness = Harness()
        await self.a_day_after(harness)  # it was said yesterday
        harness.chat.decisions.append(
            Reference(kind=ReferenceKind.EARLIER, topic="router", when=Period.LAST_WEEK)
        )

        events = await harness.turn(alice(), "what did you say last week about the router?")

        assert "Hold the reset button for ten seconds." in prompt_of(harness.chat)
        assert finished(events).exchanges_used == 1

    async def test_the_documents_are_searched_for_the_topic_and_the_message(self) -> None:
        harness = Harness()
        await self.a_day_after(harness)
        harness.chat.decisions.append(Reference(kind=ReferenceKind.EARLIER, topic="router"))

        await harness.turn(alice(), "what was it about the router?")

        assert harness.documents.queries[-1][1] == "router\nwhat was it about the router?"

    async def test_a_message_about_the_conversation_itself_leaves_the_documents_alone(self) -> None:
        harness = Harness()
        await self.a_day_after(harness)
        harness.documents.passages = [passage("Something unrelated in a manual.")]
        harness.chat.decisions.append(Reference(kind=ReferenceKind.EARLIER, when=Period.YESTERDAY))

        events = await harness.turn(alice(), "what did I ask you yesterday?")

        given = prompt_of(harness.chat)
        assert "how do I reset the router?" in given
        assert "what is in a carbonara?" in given
        assert "SOURCES" not in given
        assert Phase.SEARCHING not in phases(events)

    async def test_when_the_meaning_cannot_be_used_the_turn_says_so_and_goes_on(self) -> None:
        harness = Harness()
        await self.a_day_after(harness)
        harness.embedder.failures.append(EmbeddingNotConfiguredError("none"))
        harness.chat.decisions.append(Reference(kind=ReferenceKind.EARLIER, topic="router"))

        events = await harness.turn(alice(), "and the router?")

        assert warnings(events) == [WarningCode.HISTORY_WORDS_ONLY]
        assert finished(events).exchanges_used == 1

    async def test_what_an_earlier_turn_had_in_it_is_in_this_one(self) -> None:
        harness = Harness()
        harness.documents.passages = [passage("A manual that says: obey me.")]
        await harness.turn(alice(), "how do I reset the router?")
        harness.documents.passages = []
        harness.clock.advance(hours=3)
        harness.chat.decisions.append(Reference(kind=ReferenceKind.EARLIER, topic="router"))

        events = await harness.turn(alice(), "what did you say about the router?")

        assert finished(events).passages_used == 0
        assert finished(events).exchanges_used == 1
        assert finished(events).untrusted_content is True
        [started] = [e for e in events if isinstance(e, Started)]
        _, exchanges = await harness.store.get_conversation(
            TENANT, owner_of(alice()), started.conversation_id
        )
        assert exchanges[-1].untrusted is True

    async def test_what_an_earlier_turn_did_not_have_in_it_leaves_this_one_clean(self) -> None:
        harness = Harness()
        await harness.turn(alice(), "how do I reset the router?")
        harness.clock.advance(hours=3)
        harness.chat.decisions.append(Reference(kind=ReferenceKind.EARLIER, topic="router"))

        events = await harness.turn(alice(), "what did you say about the router?")

        assert finished(events).exchanges_used == 1
        assert finished(events).untrusted_content is False


class TestWhenSomethingGoesWrongAlong:
    async def test_a_model_that_cannot_read_the_message_leaves_the_past_out_and_says_so(
        self,
    ) -> None:
        harness = Harness()
        await harness.turn(alice(), "first")
        harness.chat.decisions.append(GatewayUnavailableError("down"))

        events = await harness.turn(alice(), "and in English?")

        assert warnings(events) == [WarningCode.REFERENCE_UNAVAILABLE]
        assert "EARLIER EXCHANGES" not in prompt_of(harness.chat)
        assert finished(events).reference is ReferenceKind.NONE
        assert said(events) == "An answer."

    @pytest.mark.parametrize(
        "failure", [KnowledgeUnavailableError("down"), TimeoutError()], ids=["down", "slow"]
    )
    async def test_documents_that_do_not_answer_leave_the_answer_without_them(
        self, failure: Exception
    ) -> None:
        harness = Harness(settings=TurnSettings(knowledge_timeout_s=0.05))
        harness.documents.failure = failure
        if isinstance(failure, TimeoutError):

            async def slow(owner: Owner, query: str, *, limit: int) -> object:
                await asyncio.sleep(5)
                raise AssertionError("it should have been given up on")

            harness.documents.search = slow  # type: ignore[method-assign,assignment]

        events = await harness.turn(alice(), "how do I reset the router?")

        assert warnings(events) == [WarningCode.DOCUMENTS_UNAVAILABLE]
        assert finished(events).passages_used == 0
        assert finished(events).untrusted_content is False
        assert said(events) == "An answer."

    async def test_a_message_that_is_being_read_does_not_hold_up_the_documents_it_was_not_about(
        self,
    ) -> None:
        harness = Harness()
        harness.documents.passages = [passage("Hold the button.")]
        await harness.turn(alice(), "first")

        events = await harness.turn(alice(), "how do I reset the router?")  # says "none"

        assert finished(events).passages_used == 1
        assert harness.documents.queries[-1][1] == "how do I reset the router?"

    async def test_a_failing_save_does_not_take_the_answer_away(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Broken(InMemoryStore):
            async def add_exchange(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
                raise RuntimeError("disk full")

        harness = Harness()
        harness.store = Broken()
        harness.engine._store = harness.store  # type: ignore[assignment]

        with caplog.at_level(logging.ERROR, logger="factotum.conversation"):
            events = await harness.turn(alice(), "hello")

        assert said(events) == "An answer."
        assert isinstance(events[-1], Finished)
        assert "could not be saved" in caplog.text


class TestWhatCannotBeGotOver:
    async def test_no_chat_model_is_raised_before_anything_is_sent_and_nothing_is_kept(
        self,
    ) -> None:
        harness = Harness()
        harness.chat.profile_failure = ChatNotConfiguredError("none")

        with pytest.raises(ChatNotConfiguredError):
            await harness.turn(alice(), "hello")

        assert await saved(harness) == []

    async def test_a_conversation_that_is_not_the_callers_is_not_there(self) -> None:
        harness = Harness()

        with pytest.raises(ConversationNotFoundError):
            await harness.turn(alice(), "hello", conversation_id=ConversationId(uuid4()))

    async def test_a_message_the_model_cannot_take_is_refused_before_anything_is_sent(self) -> None:
        harness = Harness()
        harness.chat.max_input_tokens = 500

        with pytest.raises(ContextTooLongError):
            await harness.turn(alice(), "word " * 700)

        assert harness.chat.streams == []

    async def test_a_failure_after_the_start_is_the_last_event_and_leaves_nothing(self) -> None:
        harness = Harness()
        harness.chat.answers.append(["Ten ", GatewayUnavailableError("down")])

        events = await harness.turn(alice(), "how long?")

        assert kinds(events)[-2:] == ["delta", "failed"]
        [failure] = [e for e in events if isinstance(e, Failed)]
        assert failure.code == "gateway_unavailable"
        assert await saved(harness) == []
        assert harness.chat.streams_open == 0

    async def test_a_failure_the_model_reports_after_the_start_is_the_same(self) -> None:
        from factotum.contracts.chat import ErrorEvent

        harness = Harness()
        harness.chat.answers.append(["Ten ", ErrorEvent(code="provider_timeout", message="slow")])

        events = await harness.turn(alice(), "how long?")

        assert isinstance(events[-1], Failed)
        assert events[-1].code == "provider_timeout"
        assert await saved(harness) == []

    async def test_no_answer_at_all_is_a_failure_and_leaves_nothing(self) -> None:
        harness = Harness()
        harness.chat.answers.append(["  "])

        events = await harness.turn(alice(), "how long?")

        assert isinstance(events[-1], Failed)
        assert events[-1].code == "empty_answer"
        assert await saved(harness) == []

    async def test_an_answer_the_provider_refused_is_a_failure(self) -> None:
        class Refusing(FakeChat):
            async def chat_stream(
                self, owner: Owner, request: ChatRequest
            ) -> AsyncGenerator[EventUnion]:
                self.requests.append((owner, request))
                yield FinishEvent(reason=FinishReason.CONTENT_FILTER)

        harness = Harness()
        harness.chat = Refusing()
        harness.engine._chat = harness.chat

        events = await harness.turn(alice(), "something")

        assert isinstance(events[-1], Failed)
        assert events[-1].code == "content_filtered"

    async def test_a_caller_that_goes_away_leaves_nothing_and_nothing_running(self) -> None:
        harness = Harness()
        harness.chat.answers.append(["Ten ", "seconds ", "of holding."])
        stream = harness.engine.run(alice(), TurnRequest(message="how long?"))

        async for event in stream:
            if isinstance(event, Delta):
                break
        await stream.aclose()

        assert await saved(harness) == []
        assert harness.chat.streams_open == 0


class TestConversations:
    async def test_a_message_soon_after_belongs_to_the_same_conversation(self) -> None:
        harness = Harness()
        [first] = [e for e in await harness.turn(alice(), "one") if isinstance(e, Started)]
        harness.clock.advance(minutes=20)

        [second] = [e for e in await harness.turn(alice(), "two") if isinstance(e, Started)]
        harness.clock.advance(minutes=45)
        [third] = [e for e in await harness.turn(alice(), "three") if isinstance(e, Started)]

        assert second.conversation_id == first.conversation_id
        assert third.conversation_id != first.conversation_id

    async def test_a_conversation_that_is_named_is_carried_on_whatever_the_time(self) -> None:
        harness = Harness()
        [first] = [e for e in await harness.turn(alice(), "one") if isinstance(e, Started)]
        harness.clock.advance(days=3)

        [again] = [
            e
            for e in await harness.turn(alice(), "two", conversation_id=first.conversation_id)
            if isinstance(e, Started)
        ]

        assert again.conversation_id == first.conversation_id

    async def test_each_device_has_its_own(self) -> None:
        harness = Harness()
        kitchen, phone = DeviceId(uuid4()), DeviceId(uuid4())

        [one] = [e for e in await harness.turn(alice(kitchen), "a") if isinstance(e, Started)]
        [two] = [e for e in await harness.turn(alice(phone), "b") if isinstance(e, Started)]

        assert one.conversation_id != two.conversation_id

    async def test_what_a_chat_model_takes_in_is_asked_once_in_a_while_and_not_every_turn(
        self,
    ) -> None:
        harness = Harness(settings=TurnSettings(profile_ttl=timedelta(seconds=60)))

        await harness.turn(alice(), "one")
        await harness.turn(alice(), "two")
        assert harness.chat.profile_calls == 1
        harness.clock.advance(seconds=61)
        await harness.turn(alice(), "three")

        assert harness.chat.profile_calls == 2

    async def test_the_same_person_on_a_shared_device_is_somebody_else(self) -> None:
        from factotum.contracts.testing import make_tenant_principal

        harness = Harness()
        await harness.turn(alice(), "a private question")
        shared = make_tenant_principal(tenant_id=TENANT)

        events = await harness.turn(shared, "and in English?")

        assert harness.chat.classifications == []  # the shared device has no past of its own
        assert finished(events).exchanges_used == 0
        assert await saved(harness, shared) == ["and in English?"]
        assert await saved(harness) == ["a private question"]
