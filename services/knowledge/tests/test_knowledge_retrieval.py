"""Hybrid search, against the store in memory and an embedder that needs no model."""

from uuid import uuid4

import pytest

from factotum.contracts.errors import (
    CollectionNotFoundError,
    EmbeddingNotConfiguredError,
    GatewayUnavailableError,
)
from factotum.contracts.identity import Principal, Role
from factotum.contracts.knowledge import (
    Collection,
    CollectionOwner,
    CollectionStatus,
    SearchRequest,
    SearchWarningCode,
)
from factotum.contracts.testing import make_tenant_principal, make_user_principal
from factotum.knowledge.testing import Harness

ADMIN = frozenset({Role.ADMIN})

ROUTER = (
    "To restore the factory settings of the router, hold the reset button for ten seconds "
    "until the light blinks."
)
WARRANTY = "The warranty covers manufacturing defects for two years from the day of purchase."
CATS = "Cats sleep most of the day and hunt at night."
MANUAL = "\n\n".join(
    [
        "# Networking\n\n## Routers",
        "A router forwards packets between networks and keeps a table of routes. " * 4,
        "The router table is rebuilt every time the router restarts, and it takes seconds. " * 4,
        "Router firmware is updated from the administration page of the router itself. " * 4,
        "When the router firmware is updated the router restarts and forgets its routes. " * 4,
    ]
)
"""One section of about 1200 characters: two pieces to search, and one section around them."""


def search(query: str, **options: object) -> SearchRequest:
    return SearchRequest.model_validate({"query": query, **options})


@pytest.fixture
def alice() -> Principal:
    return make_user_principal(roles=ADMIN)


@pytest.fixture
async def shop(alice: Principal) -> tuple[Harness, Collection]:
    harness = Harness()
    collection = await harness.collection(alice, "Manuals")
    await harness.add_text(alice, collection, "Router", ROUTER, language="en")
    await harness.add_text(alice, collection, "Warranty", WARRANTY, language="en")
    await harness.add_text(alice, collection, "Cats", CATS, language="en")
    await harness.process()
    return harness, collection


class TestFinding:
    async def test_a_piece_with_the_words_is_found(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, _ = shop

        result = await harness.service.search(alice, search("warranty defects"))

        assert result.passages[0].text == WARRANTY
        assert result.warnings == ()

    async def test_a_passage_says_where_it_is_from_and_that_it_is_somebody_elses_words(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, collection = shop

        [first, *_] = (await harness.service.search(alice, search("reset button"))).passages

        assert first.collection_id == collection.id
        assert (first.document_title, first.source) == ("Router", None)
        assert first.language == "en"
        assert first.untrusted is True
        assert first.text == ROUTER
        assert first.context == ROUTER

    async def test_what_both_searches_found_comes_first_and_says_so(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, _ = shop

        result = await harness.service.search(alice, search("reset the router"))

        best = result.passages[0]
        assert best.text == ROUTER
        assert best.matched_by == ("vector", "text")
        assert 0 < best.score <= 1

    async def test_a_piece_that_only_the_meaning_finds_is_still_found(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        # The model knows these two mean the same; the words of the query are nowhere in the text.
        harness.embedder.aliases = {"felines": "cats"}
        collection = await harness.collection(alice)
        await harness.add_text(alice, collection, "Router", ROUTER)
        await harness.add_text(alice, collection, "Cats", CATS)
        await harness.process()

        result = await harness.service.search(alice, search("felines"))

        assert result.passages[0].text == CATS
        assert result.passages[0].matched_by == ("vector",)

    async def test_the_limit_is_respected(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, _ = shop

        result = await harness.service.search(
            alice, search("the router warranty cats reset", limit=2)
        )

        assert len(result.passages) == 2

    async def test_a_question_about_something_that_is_nowhere_finds_no_words_at_all(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, _ = shop

        result = await harness.service.search(alice, search("submarine"))

        # The nearest pieces by meaning always come back, and say that they were found by meaning
        # alone: that is how a caller tells a strong answer from a weak one.
        assert result.passages
        assert all(p.matched_by == ("vector",) for p in result.passages)

    async def test_the_same_question_always_gets_the_same_answer(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, _ = shop

        first = await harness.service.search(alice, search("router reset warranty"))
        second = await harness.service.search(alice, search("router reset warranty"))

        assert first == second

    async def test_the_pieces_of_one_section_are_one_passage_with_the_section_as_context(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        collection = await harness.collection(alice)
        manual = await harness.add_text(alice, collection, "Networking", MANUAL, language="en")
        await harness.process()

        result = await harness.service.search(alice, search("router firmware restarts"))

        assert (await harness.service.get_document(alice, manual.id)).chunk_count == 2
        assert len(result.passages) == 1
        assert "forwards packets" in result.passages[0].context
        assert "firmware" in result.passages[0].context


class TestWhereItLooks:
    async def test_it_looks_only_at_the_collections_asked_for(self, alice: Principal) -> None:
        harness = Harness()
        first = await harness.collection(alice, "First")
        second = await harness.collection(alice, "Second")
        await harness.add_text(alice, first, "One", "The router boots slowly.")
        await harness.add_text(alice, second, "Two", "The router boots quickly.")
        await harness.process()

        result = await harness.service.search(alice, search("router", collection_ids=[second.id]))

        assert {p.collection_id for p in result.passages} == {second.id}

    async def test_it_looks_at_every_collection_the_person_may_see_by_default(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        first = await harness.collection(alice, "First")
        second = await harness.collection(alice, "Second")
        await harness.add_text(alice, first, "One", "The router boots slowly.")
        await harness.add_text(alice, second, "Two", "The router boots quickly.")
        await harness.process()

        result = await harness.service.search(alice, search("router"))

        assert {p.collection_id for p in result.passages} == {first.id, second.id}

    async def test_a_person_does_not_find_what_is_another_persons(self, alice: Principal) -> None:
        harness = Harness()
        hers = await harness.collection(alice, "Private")
        await harness.add_text(alice, hers, "Diary", "The router password is hunter2.")
        await harness.process()
        bob = make_user_principal(tenant_id=alice.tenant_id)

        result = await harness.service.search(bob, search("router password"))

        assert result.passages == ()

    async def test_a_collection_of_the_tenant_is_found_by_everybody_in_it(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        shared = await harness.collection(alice, "Household", owner=CollectionOwner.TENANT)
        await harness.add_text(alice, shared, "Rules", "The router password is on the fridge.")
        await harness.process()
        bob = make_user_principal(tenant_id=alice.tenant_id)
        speaker = make_tenant_principal(tenant_id=alice.tenant_id)

        for asking in (bob, speaker):
            result = await harness.service.search(asking, search("router password"))
            assert [p.collection_id for p in result.passages] == [shared.id]

    async def test_asking_for_a_collection_that_may_not_be_seen_is_asking_for_one_that_is_not_there(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        hers = await harness.collection(alice, "Private")
        bob = make_user_principal(tenant_id=alice.tenant_id)

        with pytest.raises(CollectionNotFoundError):
            await harness.service.search(bob, search("anything", collection_ids=[hers.id]))
        with pytest.raises(CollectionNotFoundError):
            await harness.service.search(alice, search("anything", collection_ids=[uuid4()]))

    async def test_a_person_with_no_collections_gets_an_empty_answer_without_asking_a_model(
        self, alice: Principal
    ) -> None:
        harness = Harness()

        result = await harness.service.search(alice, search("anything"))

        assert (result.passages, result.warnings) == ((), ())
        assert harness.embedder.calls == []

    async def test_a_tenant_never_finds_what_is_in_another_tenant(self, alice: Principal) -> None:
        harness = Harness()
        mine = await harness.collection(alice, "Mine")
        await harness.add_text(alice, mine, "Secret", "The vault code is 1234.")
        await harness.process()
        stranger = make_user_principal(roles=ADMIN)

        result = await harness.service.search(stranger, search("vault code"))

        assert result.passages == ()


class TestOneQueryOneEmbeddingPerOwner:
    async def test_the_query_is_embedded_once_for_each_owner_and_as_that_owner(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        mine = await harness.collection(alice, "Mine")
        also_mine = await harness.collection(alice, "Also mine")
        shared = await harness.collection(alice, "Shared", owner=CollectionOwner.TENANT)
        for collection in (mine, also_mine, shared):
            await harness.add_text(alice, collection, "Doc", "The router boots.")
        await harness.process()
        harness.embedder.calls.clear()

        await harness.service.search(alice, search("router"))

        assert [texts for _, texts in harness.embedder.calls] == [["router"], ["router"]]
        assert {owner.kind.value for owner, _ in harness.embedder.calls} == {"user", "tenant"}

    async def test_the_best_of_all_the_collections_of_one_model_come_first(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        first = await harness.collection(alice, "First")
        second = await harness.collection(alice, "Second")
        await harness.add_text(alice, first, "Vague", "Something about a router somewhere.")
        await harness.add_text(alice, second, "Exact", "Restore the router to factory settings.")
        await harness.process()

        result = await harness.service.search(alice, search("restore router factory settings"))

        assert result.passages[0].document_title == "Exact"


class TestWhenSearchingByMeaningIsNotPossible:
    async def collection_with_a_document(self, harness: Harness, alice: Principal) -> Collection:
        collection = await harness.collection(alice)
        await harness.add_text(alice, collection, "Router", ROUTER)
        await harness.process()
        return collection

    async def test_a_gateway_that_does_not_answer_still_leaves_the_words(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        collection = await self.collection_with_a_document(harness, alice)
        harness.embedder.failures = [GatewayUnavailableError("down")]

        result = await harness.service.search(alice, search("reset button"))

        assert result.passages[0].text == ROUTER
        assert result.passages[0].matched_by == ("text",)
        [warning] = result.warnings
        assert (warning.collection_id, warning.code) == (
            collection.id,
            SearchWarningCode.VECTOR_UNAVAILABLE,
        )

    async def test_an_owner_without_an_embedding_model_still_gets_the_words(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        await self.collection_with_a_document(harness, alice)
        harness.embedder.failures = [EmbeddingNotConfiguredError("no binding")]

        result = await harness.service.search(alice, search("reset button"))

        assert result.passages
        assert [w.code for w in result.warnings] == [SearchWarningCode.EMBEDDING_NOT_CONFIGURED]

    async def test_a_model_that_changed_is_noticed_and_the_collection_is_searched_by_words(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        collection = await self.collection_with_a_document(harness, alice)
        harness.embedder.space_id = "emb-another-model"

        result = await harness.service.search(alice, search("reset button"))

        assert result.passages[0].matched_by == ("text",)
        assert [w.code for w in result.warnings] == [SearchWarningCode.NEEDS_REINDEX]
        marked = await harness.service.get_collection(alice, collection.id)
        assert marked.status is CollectionStatus.NEEDS_REINDEX

    async def test_a_collection_that_needs_reindexing_is_not_embedded_for_again(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        await self.collection_with_a_document(harness, alice)
        harness.embedder.space_id = "emb-another-model"
        await harness.service.search(alice, search("reset button"))
        harness.embedder.calls.clear()

        result = await harness.service.search(alice, search("reset button"))

        assert harness.embedder.calls == []
        assert [w.code for w in result.warnings] == [SearchWarningCode.NEEDS_REINDEX]
        assert result.passages

    async def test_reindexing_makes_the_search_by_meaning_work_again(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        collection = await self.collection_with_a_document(harness, alice)
        harness.embedder.space_id = "emb-another-model"
        await harness.service.search(alice, search("reset button"))

        await harness.service.reindex_collection(alice, collection.id)
        await harness.process()
        result = await harness.service.search(alice, search("reset button"))

        assert result.warnings == ()
        assert result.passages[0].matched_by == ("vector", "text")

    async def test_a_collection_being_reindexed_says_so(self, alice: Principal) -> None:
        harness = Harness()
        collection = await self.collection_with_a_document(harness, alice)
        await harness.store.begin_reindex(
            alice.tenant_id, collection.id, space_id="emb-fake", dimensions=64
        )

        result = await harness.service.search(alice, search("reset button"))

        [warning] = result.warnings
        assert warning.code is SearchWarningCode.NEEDS_REINDEX
        assert "being indexed again" in warning.message
        assert result.passages

    async def test_one_collection_that_cannot_be_searched_by_meaning_does_not_spoil_another(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        old = await self.collection_with_a_document(harness, alice)
        await harness.store.mark_needs_reindex(alice.tenant_id, old.id)
        fresh = await harness.collection(alice, "Fresh")
        await harness.add_text(alice, fresh, "Warranty", WARRANTY)
        await harness.process()

        result = await harness.service.search(alice, search("warranty defects"))

        assert result.passages[0].text == WARRANTY
        assert result.passages[0].matched_by == ("vector", "text")
        assert [w.collection_id for w in result.warnings] == [old.id]
