import pytest

from factotum.contracts.conversation import (
    Delta,
    Finished,
    HistoryQuery,
    Period,
    Started,
    TurnEvent,
    TurnRequest,
    WarningCode,
)
from factotum.contracts.errors import (
    ConversationNotFoundError,
    EmbeddingNotConfiguredError,
    PolicyViolationError,
)
from factotum.contracts.identity import Principal
from factotum.contracts.ids import ConversationId
from factotum.contracts.testing import make_tenant_id, make_tenant_principal, make_user_principal
from factotum.conversation.service import ConversationService
from factotum.conversation.testing import Harness, passage

TENANT = make_tenant_id()
ALICE = make_user_principal(tenant_id=TENANT)
BOB = make_user_principal(tenant_id=TENANT)
SHARED = make_tenant_principal(tenant_id=TENANT)


def rig() -> tuple[Harness, ConversationService]:
    harness = Harness()
    return harness, ConversationService(harness.store, harness.engine, harness.history)


async def talk(service: ConversationService, who: Principal, message: str) -> list[TurnEvent]:
    return [e async for e in service.turn(who, TurnRequest(message=message))]


def conversation_of(events: list[TurnEvent]) -> ConversationId:
    [started] = [e for e in events if isinstance(e, Started)]
    return started.conversation_id


class TestAnybodyMayTalk:
    @pytest.mark.parametrize("who", [ALICE, SHARED], ids=["a person", "a shared device"])
    async def test_a_turn_is_answered_for_the_subject_the_caller_acts_for(
        self, who: Principal
    ) -> None:
        harness, service = rig()
        harness.documents.passages = [passage()]

        events = await talk(service, who, "hello")

        assert "".join(e.text for e in events if isinstance(e, Delta)) == "An answer."
        assert any(isinstance(e, Finished) for e in events)


class TestAPersonReadsAndForgetsTheirOwn:
    async def test_their_conversations_are_listed_and_read(self) -> None:
        _, service = rig()
        conversation = conversation_of(await talk(service, ALICE, "how do I reset it?"))

        listed = await service.list_conversations(ALICE, limit=10, offset=0)
        detail = await service.get_conversation(ALICE, conversation)

        assert [c.id for c in listed.conversations] == [conversation]
        assert [e.question for e in detail.exchanges] == ["how do I reset it?"]
        assert detail.conversation.exchanges == 1

    async def test_what_somebody_else_said_is_not_there(self) -> None:
        _, service = rig()
        conversation = conversation_of(await talk(service, ALICE, "a private question"))

        assert (await service.list_conversations(BOB, limit=10, offset=0)).conversations == ()
        with pytest.raises(ConversationNotFoundError):
            await service.get_conversation(BOB, conversation)
        with pytest.raises(ConversationNotFoundError):
            await service.delete_conversation(BOB, conversation)
        assert (await service.get_conversation(ALICE, conversation)).conversation.exchanges == 1

    async def test_a_conversation_can_be_forgotten(self) -> None:
        _, service = rig()
        conversation = conversation_of(await talk(service, ALICE, "one"))

        await service.delete_conversation(ALICE, conversation)

        with pytest.raises(ConversationNotFoundError):
            await service.get_conversation(ALICE, conversation)

    async def test_everything_can_be_forgotten_at_once_and_nobody_elses(self) -> None:
        harness, service = rig()
        await talk(service, ALICE, "one")
        harness.clock.advance(hours=2)
        await talk(service, ALICE, "two")
        await talk(service, BOB, "not alice's")

        forgotten = await service.forget_everything(ALICE)

        assert forgotten == 2
        assert (await service.list_conversations(ALICE, limit=10, offset=0)).conversations == ()
        assert len((await service.list_conversations(BOB, limit=10, offset=0)).conversations) == 1

    async def test_what_was_said_can_be_searched(self) -> None:
        harness, service = rig()
        await talk(service, ALICE, "how do I reset the router?")
        await talk(service, ALICE, "what is in a carbonara?")
        await harness.history.embed_pending()

        found = await service.search_history(ALICE, HistoryQuery(query="router"))

        assert [h.exchange.question for h in found.hits] == ["how do I reset the router?"]
        assert found.warnings == ()

    async def test_a_search_can_be_of_a_period_alone(self) -> None:
        harness, service = rig()
        await talk(service, ALICE, "an old one")
        harness.clock.advance(days=1)
        await talk(service, ALICE, "a new one")

        found = await service.search_history(ALICE, HistoryQuery(when=Period.YESTERDAY))

        assert [h.exchange.question for h in found.hits] == ["an old one"]

    async def test_a_search_says_when_the_meaning_could_not_be_used(self) -> None:
        harness, service = rig()
        await talk(service, ALICE, "the router")
        harness.embedder.failures.append(EmbeddingNotConfiguredError("none"))

        found = await service.search_history(ALICE, HistoryQuery(query="router"))

        assert [w.code for w in found.warnings] == [WarningCode.HISTORY_WORDS_ONLY]

    async def test_nobody_elses_history_is_searched(self) -> None:
        _, service = rig()
        await talk(service, BOB, "the router")

        found = await service.search_history(ALICE, HistoryQuery(query="router"))

        assert found.hits == ()


class TestASharedDeviceIsNoWayToReadWhatWasSaid:
    async def test_it_can_be_remembered_but_not_asked_what_it_remembers(self) -> None:
        _, service = rig()
        conversation = conversation_of(await talk(service, SHARED, "hello"))

        with pytest.raises(PolicyViolationError, match="only a person"):
            await service.list_conversations(SHARED, limit=10, offset=0)
        with pytest.raises(PolicyViolationError):
            await service.get_conversation(SHARED, conversation)
        with pytest.raises(PolicyViolationError):
            await service.delete_conversation(SHARED, conversation)
        with pytest.raises(PolicyViolationError):
            await service.forget_everything(SHARED)
        with pytest.raises(PolicyViolationError):
            await service.search_history(SHARED, HistoryQuery(query="hello"))

    async def test_what_a_shared_device_heard_is_not_what_a_person_reads(self) -> None:
        _, service = rig()
        await talk(service, SHARED, "said in the kitchen")

        assert (await service.list_conversations(ALICE, limit=10, offset=0)).conversations == ()
