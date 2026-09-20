"""What every store promises, run against the one in memory and the one in Postgres."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from factotum.contracts.conversation import Citation, Conversation, Exchange
from factotum.contracts.errors import ConversationNotFoundError
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import (
    CollectionId,
    ConversationId,
    DeviceId,
    DocumentId,
    ExchangeId,
    TenantId,
)
from factotum.conversation.store import ConversationStore, Vector

pytestmark = pytest.mark.asyncio(loop_scope="module")

NOW = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)
GAP = timedelta(minutes=30)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId(uuid4())


@pytest.fixture
def owner() -> Owner:
    return Owner(kind=OwnerKind.USER, id=uuid4())


def someone_else() -> Owner:
    return Owner(kind=OwnerKind.USER, id=uuid4())


async def open_one(
    store: ConversationStore,
    tenant: TenantId,
    owner: Owner,
    *,
    device: DeviceId | None = None,
    at: datetime = NOW,
    name: ConversationId | None = None,
) -> Conversation:
    return await store.open_conversation(
        tenant, owner, device_id=device, conversation_id=name, now=at, session_gap=GAP
    )


async def say(
    store: ConversationStore,
    tenant: TenantId,
    owner: Owner,
    conversation: Conversation,
    question: str,
    answer: str = "An answer.",
    *,
    at: datetime = NOW,
    untrusted: bool = False,
    citations: Sequence[Citation] = (),
) -> Exchange:
    return await store.add_exchange(
        tenant,
        owner,
        conversation_id=conversation.id,
        exchange_id=ExchangeId(uuid4()),
        question=question,
        answer=answer,
        citations=citations,
        untrusted=untrusted,
        now=at,
    )


def citation() -> Citation:
    return Citation(
        number=1,
        document_id=DocumentId(uuid4()),
        collection_id=CollectionId(uuid4()),
        document_title="Router manual",
        page=12,
        headings=("Router", "Reset"),
    )


class TestOpeningAConversation:
    async def test_somebody_new_gets_a_new_conversation(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)

        assert (conversation.started_at, conversation.last_at) == (NOW, NOW)
        assert (conversation.exchanges, conversation.device_id) == (0, None)

    async def test_the_conversation_that_was_active_lately_is_carried_on(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        first = await open_one(store, tenant, owner)

        again = await open_one(store, tenant, owner, at=NOW + timedelta(minutes=29))
        later = await open_one(store, tenant, owner, at=NOW + timedelta(minutes=31))

        assert again.id == first.id
        assert later.id != first.id

    async def test_what_was_said_keeps_a_conversation_going(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        first = await open_one(store, tenant, owner)
        await say(store, tenant, owner, first, "hello", at=NOW + timedelta(minutes=25))

        still = await open_one(store, tenant, owner, at=NOW + timedelta(minutes=50))

        assert still.id == first.id

    async def test_each_device_has_a_conversation_of_its_own(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        kitchen, phone = DeviceId(uuid4()), DeviceId(uuid4())

        on_kitchen = await open_one(store, tenant, owner, device=kitchen)
        on_phone = await open_one(store, tenant, owner, device=phone)
        without = await open_one(store, tenant, owner)

        assert len({on_kitchen.id, on_phone.id, without.id}) == 3
        assert (await open_one(store, tenant, owner, device=kitchen)).id == on_kitchen.id
        assert on_kitchen.device_id == kitchen

    async def test_each_owner_and_each_tenant_has_a_conversation_of_its_own(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        mine = await open_one(store, tenant, owner)

        assert (await open_one(store, tenant, someone_else())).id != mine.id
        assert (await open_one(store, TenantId(uuid4()), owner)).id != mine.id

    async def test_a_conversation_that_is_named_is_the_one_asked_for_whatever_its_age(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        old = await open_one(store, tenant, owner)

        named = await open_one(store, tenant, owner, at=NOW + timedelta(days=9), name=old.id)

        assert named.id == old.id

    async def test_a_conversation_that_is_not_the_callers_is_not_there(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        theirs = await open_one(store, tenant, someone_else())
        elsewhere = await open_one(store, TenantId(uuid4()), owner)

        for stranger in (theirs.id, elsewhere.id, ConversationId(uuid4())):
            with pytest.raises(ConversationNotFoundError):
                await open_one(store, tenant, owner, name=stranger)


class TestSavingWhatWasSaid:
    async def test_the_exchanges_of_a_conversation_are_numbered_in_order(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)

        first = await say(store, tenant, owner, conversation, "one")
        second = await say(store, tenant, owner, conversation, "two")

        assert (first.seq, second.seq) == (1, 2)
        found, exchanges = await store.get_conversation(tenant, owner, conversation.id)
        assert found.exchanges == 2
        assert [e.question for e in exchanges] == ["one", "two"]

    async def test_saving_moves_the_last_activity_forward_and_never_back(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        await say(store, tenant, owner, conversation, "later", at=NOW + timedelta(minutes=10))
        await say(store, tenant, owner, conversation, "earlier", at=NOW + timedelta(minutes=5))

        found, _ = await store.get_conversation(tenant, owner, conversation.id)

        assert found.last_at == NOW + timedelta(minutes=10)

    async def test_the_documents_cited_and_the_mark_of_somebody_elses_words_are_kept(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        cited = citation()
        await say(store, tenant, owner, conversation, "q", untrusted=True, citations=(cited,))
        await say(store, tenant, owner, conversation, "plain")

        _, exchanges = await store.get_conversation(tenant, owner, conversation.id)

        assert (exchanges[0].untrusted, exchanges[0].citations) == (True, (cited,))
        assert (exchanges[1].untrusted, exchanges[1].citations) == (False, ())

    async def test_the_same_question_can_be_asked_twice(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)

        await say(store, tenant, owner, conversation, "what time is it?")
        await say(store, tenant, owner, conversation, "what time is it?")

        assert (await store.get_conversation(tenant, owner, conversation.id))[0].exchanges == 2

    async def test_nothing_is_added_to_the_conversation_of_somebody_else(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        theirs = someone_else()
        conversation = await open_one(store, tenant, theirs)

        with pytest.raises(ConversationNotFoundError):
            await say(store, tenant, owner, conversation, "sneaky")

        assert not await store.has_exchanges(tenant, theirs)

    async def test_whether_anything_was_said_is_known_for_the_owner_alone(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        assert not await store.has_exchanges(tenant, owner)
        await say(store, tenant, owner, await open_one(store, tenant, owner), "hello")

        assert await store.has_exchanges(tenant, owner)
        assert not await store.has_exchanges(tenant, someone_else())
        assert not await store.has_exchanges(TenantId(uuid4()), owner)

    async def test_the_last_exchanges_come_oldest_first(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        for word in ("one", "two", "three", "four"):
            await say(store, tenant, owner, conversation, word)

        last = await store.last_exchanges(tenant, owner, conversation.id, limit=3)
        everything = await store.last_exchanges(tenant, owner, conversation.id, limit=10)

        assert [e.question for e in last] == ["two", "three", "four"]
        assert len(everything) == 4

    async def test_the_last_exchanges_of_somebody_elses_conversation_are_not_there(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        theirs = await open_one(store, tenant, someone_else())

        with pytest.raises(ConversationNotFoundError):
            await store.last_exchanges(tenant, owner, theirs.id, limit=3)


class TestManagingConversations:
    async def test_the_conversations_come_last_active_first_and_are_the_owners_alone(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        old = await open_one(store, tenant, owner, at=NOW)
        recent = await open_one(store, tenant, owner, at=NOW + timedelta(hours=2))
        await open_one(store, tenant, someone_else(), at=NOW + timedelta(hours=3))

        listed = await store.list_conversations(tenant, owner, limit=10, offset=0)

        assert [c.id for c in listed] == [recent.id, old.id]

    async def test_the_list_can_be_read_a_page_at_a_time(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        made = [await open_one(store, tenant, owner, at=NOW + timedelta(hours=n)) for n in range(3)]

        first = await store.list_conversations(tenant, owner, limit=2, offset=0)
        second = await store.list_conversations(tenant, owner, limit=2, offset=2)

        assert [c.id for c in first] == [made[2].id, made[1].id]
        assert [c.id for c in second] == [made[0].id]

    async def test_a_conversation_of_somebody_else_cannot_be_read(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        theirs = await open_one(store, tenant, someone_else())

        with pytest.raises(ConversationNotFoundError):
            await store.get_conversation(tenant, owner, theirs.id)

    async def test_deleting_a_conversation_forgets_what_was_said_in_it_and_only_that(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        gone = await open_one(store, tenant, owner, at=NOW)
        kept = await open_one(store, tenant, owner, at=NOW + timedelta(hours=2))
        await say(store, tenant, owner, gone, "the router recipe")
        await say(store, tenant, owner, kept, "the warranty", at=NOW + timedelta(hours=2))

        await store.delete_conversation(tenant, owner, gone.id)

        with pytest.raises(ConversationNotFoundError):
            await store.get_conversation(tenant, owner, gone.id)
        found = await store.search_words(tenant, owner, "router", since=None, until=None, limit=5)
        assert found == []
        assert (await store.get_conversation(tenant, owner, kept.id))[0].exchanges == 1

    async def test_the_conversation_of_somebody_else_cannot_be_deleted(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        theirs_owner = someone_else()
        theirs = await open_one(store, tenant, theirs_owner)

        with pytest.raises(ConversationNotFoundError):
            await store.delete_conversation(tenant, owner, theirs.id)

        assert (await store.get_conversation(tenant, theirs_owner, theirs.id))[0].id == theirs.id

    async def test_everything_an_owner_said_can_be_forgotten_at_once(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        other = someone_else()
        for at in (NOW, NOW + timedelta(hours=2)):
            await say(store, tenant, owner, await open_one(store, tenant, owner, at=at), "x", at=at)
        await say(store, tenant, other, await open_one(store, tenant, other), "y")

        forgotten = await store.delete_all(tenant, owner)

        assert forgotten == 2
        assert not await store.has_exchanges(tenant, owner)
        assert await store.has_exchanges(tenant, other)

    async def test_what_is_older_than_the_retention_is_forgotten_and_nothing_else(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        long_ago = datetime(2020, 1, 1, tzinfo=UTC)
        old = await open_one(store, tenant, owner, at=long_ago)
        await say(store, tenant, owner, old, "ancient", at=long_ago)
        recent = await open_one(store, tenant, owner, at=NOW)
        await say(store, tenant, owner, recent, "fresh")

        forgotten = await store.purge_before(datetime(2021, 1, 1, tzinfo=UTC))

        assert forgotten >= 1  # in a shared database there may be older data than this test's
        assert [c.id for c in await store.list_conversations(tenant, owner, limit=5, offset=0)] == [
            recent.id
        ]


class TestSearchingByTime:
    async def test_the_exchanges_of_a_period_come_latest_first_and_the_end_is_not_in_it(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        for hours, word in ((0, "before"), (1, "first"), (2, "second"), (3, "after")):
            await say(store, tenant, owner, conversation, word, at=NOW + timedelta(hours=hours))

        inside = await store.in_period(
            tenant,
            owner,
            since=NOW + timedelta(hours=1),
            until=NOW + timedelta(hours=3),
            limit=10,
        )

        assert [e.question for e in inside] == ["second", "first"]

    async def test_it_can_be_limited_and_left_open_at_both_ends(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        for hours in range(3):
            await say(
                store, tenant, owner, conversation, f"q{hours}", at=NOW + timedelta(hours=hours)
            )

        latest = await store.in_period(tenant, owner, since=None, until=None, limit=2)

        assert [e.question for e in latest] == ["q2", "q1"]

    async def test_only_the_owners_own_exchanges_are_in_it(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        other = someone_else()
        await say(store, tenant, other, await open_one(store, tenant, other), "theirs")

        assert await store.in_period(tenant, owner, since=None, until=None, limit=5) == []


class TestSearchingByWords:
    async def test_an_exchange_with_the_word_in_its_question_or_its_answer_is_found(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        await say(
            store, tenant, owner, conversation, "how do I reset the router?", "Hold the button."
        )
        await say(store, tenant, owner, conversation, "what is a good pasta?", "Try carbonara.")
        await say(store, tenant, owner, conversation, "and the warranty?", "It covers the router.")

        found = await store.search_words(tenant, owner, "router", since=None, until=None, limit=5)

        assert {f.exchange.question for f in found} == {
            "how do I reset the router?",
            "and the warranty?",
        }

    async def test_capitals_and_accents_do_not_matter(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        await say(store, tenant, owner, conversation, "Com'è il tempo a Città del Messico?")

        found = await store.search_words(
            tenant, owner, "CITTA MESSICO", since=None, until=None, limit=5
        )

        assert len(found) == 1

    async def test_several_words_are_alternatives_and_what_has_more_of_them_comes_first(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        await say(store, tenant, owner, conversation, "the router alone")
        await say(store, tenant, owner, conversation, "the router and the firmware")

        found = await store.search_words(
            tenant, owner, "router firmware", since=None, until=None, limit=5
        )

        assert [f.exchange.question for f in found] == [
            "the router and the firmware",
            "the router alone",
        ]

    async def test_nothing_is_found_when_no_word_is_there(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        await say(store, tenant, owner, await open_one(store, tenant, owner), "the router")

        assert (
            await store.search_words(tenant, owner, "submarine", since=None, until=None, limit=5)
            == []
        )

    async def test_it_looks_only_in_the_period_asked_for(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        await say(store, tenant, owner, conversation, "router monday", at=NOW)
        await say(store, tenant, owner, conversation, "router tuesday", at=NOW + timedelta(days=1))

        found = await store.search_words(
            tenant,
            owner,
            "router",
            since=NOW + timedelta(hours=12),
            until=NOW + timedelta(days=2),
            limit=5,
        )

        assert [f.exchange.question for f in found] == ["router tuesday"]

    async def test_it_looks_only_in_what_the_owner_said(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        other = someone_else()
        elsewhere = TenantId(uuid4())
        await say(store, tenant, other, await open_one(store, tenant, other), "router")
        await say(store, elsewhere, owner, await open_one(store, elsewhere, owner), "router")

        assert (
            await store.search_words(tenant, owner, "router", since=None, until=None, limit=5) == []
        )


class TestSearchingByMeaning:
    async def vectors(
        self,
        store: ConversationStore,
        tenant: TenantId,
        owner: Owner,
        *said: tuple[str, tuple[float, ...]],
    ) -> list[Exchange]:
        conversation = await open_one(store, tenant, owner)
        made = [await say(store, tenant, owner, conversation, question) for question, _ in said]
        await store.store_embeddings(
            [
                Vector(e.id, tenant, "emb-1", vector)
                for e, (_, vector) in zip(made, said, strict=True)
            ]
        )
        return made

    async def test_what_is_nearest_comes_first(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        await self.vectors(
            store,
            tenant,
            owner,
            ("north", (1.0, 0.0, 0.0)),
            ("east", (0.0, 1.0, 0.0)),
            ("northeast", (1.0, 1.0, 0.0)),
        )

        found = await store.search_vector(
            tenant, owner, "emb-1", (0.9, 0.1, 0.0), since=None, until=None, limit=3
        )

        assert [f.exchange.question for f in found] == ["north", "northeast", "east"]
        assert found[0].score > found[1].score > found[2].score

    async def test_only_vectors_of_the_space_asked_for_are_compared(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        here = await say(store, tenant, owner, conversation, "here")
        there = await say(store, tenant, owner, conversation, "there")
        await say(store, tenant, owner, conversation, "without a vector")
        await store.store_embeddings(
            [
                Vector(here.id, tenant, "emb-1", (1.0, 0.0)),
                Vector(there.id, tenant, "emb-2", (1.0, 0.0)),
            ]
        )

        found = await store.search_vector(
            tenant, owner, "emb-1", (1.0, 0.0), since=None, until=None, limit=5
        )

        assert [f.exchange.question for f in found] == ["here"]

    async def test_it_looks_only_in_the_period_and_in_what_the_owner_said(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        await self.vectors(store, tenant, owner, ("mine", (1.0, 0.0)))
        other = someone_else()
        await self.vectors(store, tenant, other, ("theirs", (1.0, 0.0)))

        mine = await store.search_vector(
            tenant, owner, "emb-1", (1.0, 0.0), since=None, until=None, limit=5
        )
        too_late = await store.search_vector(
            tenant, owner, "emb-1", (1.0, 0.0), since=NOW + timedelta(days=1), until=None, limit=5
        )

        assert [f.exchange.question for f in mine] == ["mine"]
        assert too_late == []


class TestTheVectors:
    async def owned_pending(self, store: ConversationStore, tenant: TenantId) -> list[str]:
        pending = await store.pending_embeddings(limit=1000, now=NOW + timedelta(days=30))
        return [p.question for p in pending if p.tenant_id == tenant]

    async def test_what_has_no_vector_is_waiting_for_one_oldest_first(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        await say(store, tenant, owner, conversation, "second", at=NOW + timedelta(minutes=1))
        await say(store, tenant, owner, conversation, "first", at=NOW)

        assert await self.owned_pending(store, tenant) == ["first", "second"]

    async def test_it_says_who_the_exchange_is_for_and_what_was_said(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        await say(store, tenant, owner, await open_one(store, tenant, owner), "q", "a")

        pending = [
            p
            for p in await store.pending_embeddings(limit=1000, now=NOW + timedelta(days=30))
            if p.tenant_id == tenant
        ]

        assert [(p.owner, p.question, p.answer) for p in pending] == [(owner, "q", "a")]

    async def test_a_vector_that_is_stored_is_no_longer_waited_for(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        exchange = await say(store, tenant, owner, await open_one(store, tenant, owner), "q")

        await store.store_embeddings([Vector(exchange.id, tenant, "emb-1", (1.0, 0.0))])

        assert await self.owned_pending(store, tenant) == []

    async def test_what_is_left_for_later_is_not_waited_for_until_then(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        exchange = await say(store, tenant, owner, await open_one(store, tenant, owner), "q")
        later = NOW + timedelta(hours=1)

        await store.postpone_embeddings([exchange.id], later)

        before = await store.pending_embeddings(limit=1000, now=NOW)
        after = await store.pending_embeddings(limit=1000, now=later)
        assert all(p.id != exchange.id for p in before)
        assert any(p.id == exchange.id for p in after)

    async def test_how_many_are_waited_for_at_once_is_bounded(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        for n in range(3):
            await say(store, tenant, owner, conversation, f"q{n}")

        assert len(await store.pending_embeddings(limit=2, now=NOW + timedelta(days=30))) == 2

    async def test_a_vector_for_an_exchange_of_another_tenant_is_ignored(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        exchange = await say(store, tenant, owner, await open_one(store, tenant, owner), "q")

        await store.store_embeddings([Vector(exchange.id, TenantId(uuid4()), "emb-1", (1.0, 0.0))])

        assert await self.owned_pending(store, tenant) == ["q"]

    async def test_what_a_model_that_is_no_longer_the_owners_made_is_made_again(
        self, store: ConversationStore, tenant: TenantId, owner: Owner
    ) -> None:
        conversation = await open_one(store, tenant, owner)
        old = await say(store, tenant, owner, conversation, "old")
        current = await say(store, tenant, owner, conversation, "current")
        other = someone_else()
        theirs = await say(store, tenant, other, await open_one(store, tenant, other), "theirs")
        await store.store_embeddings(
            [
                Vector(old.id, tenant, "emb-old", (1.0, 0.0)),
                Vector(current.id, tenant, "emb-new", (1.0, 0.0)),
                Vector(theirs.id, tenant, "emb-old", (1.0, 0.0)),
            ]
        )

        forgotten = await store.forget_other_spaces(tenant, owner, "emb-new")

        assert forgotten == 1
        assert await self.owned_pending(store, tenant) == ["old"]
        assert (
            await store.search_vector(
                tenant, owner, "emb-old", (1.0, 0.0), since=None, until=None, limit=5
            )
            == []
        )
        # what belongs to somebody else keeps its vector: it is theirs to keep or to change
        assert (
            len(
                await store.search_vector(
                    tenant, other, "emb-old", (1.0, 0.0), since=None, until=None, limit=5
                )
            )
            == 1
        )
