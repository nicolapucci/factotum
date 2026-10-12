"""Hybrid search, against the store in memory and an embedder that needs no model."""

from uuid import UUID, uuid4

import pytest

from factotum.contracts.errors import (
    CollectionNotFoundError,
    EmbeddingNotConfiguredError,
    GatewayUnavailableError,
)
from factotum.contracts.identity import Principal, Role
from factotum.contracts.ids import ChunkId, CollectionId, DocumentId
from factotum.contracts.knowledge import (
    Collection,
    CollectionOwner,
    CollectionStatus,
    SearchRequest,
    SearchWarningCode,
)
from factotum.contracts.testing import make_tenant_principal, make_user_principal
from factotum.knowledge.retrieval import Retriever, _ByMeaning, names_a_code, words_weight
from factotum.knowledge.store import Hit
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


def hit(name: str, parent: UUID | None = None, score: float = 1.0) -> Hit:
    return Hit(
        chunk_id=ChunkId(uuid4()),
        collection_id=CollectionId(uuid4()),
        document_id=DocumentId(uuid4()),
        parent_id=uuid4() if parent is None else parent,
        document_title=name,
        source=None,
        text=name,
        context=name,
        headings=(),
        page=None,
        language=None,
        score=score,
    )


class TestHowTheWordsCount:
    @pytest.mark.parametrize(
        "query",
        [
            "router",
            "reset button",
            "ssh-keygen",
            "chmod 755",
            "file.txt",
            "E-4021",
            "192.168.1.1",
            "error 404 not found here",
            "path/to/thing is missing",
            "snake_case is what it is called",
        ],
    )
    def test_a_name_or_a_couple_of_words_or_a_code_count_as_much_as_the_meaning(
        self, query: str
    ) -> None:
        assert words_weight(query) == 1.0

    @pytest.mark.parametrize(
        "query",
        [
            "how do I restore the router to factory settings?",
            "what does the warranty not cover",
            "come faccio a ripristinare il router alle impostazioni di fabbrica?",
            "I want to reset the router.",
            "does it work at all, really?",
        ],
    )
    def test_a_question_counts_for_half(self, query: str) -> None:
        assert words_weight(query) == 0.5

    def test_a_full_stop_at_the_end_of_a_word_is_not_a_code(self) -> None:
        assert words_weight("please reset the router.") == 0.5

    def test_what_only_the_words_found_is_buried_under_a_question_and_seen_under_a_name(
        self,
    ) -> None:
        only_words = hit("found by the words alone")
        meaning = [hit(f"found by meaning {n}") for n in range(8)]
        found = _ByMeaning(space_id="emb", hits=meaning)

        as_a_name = Retriever._passages([only_words], [found], limit=8, words=1.0).passages
        as_a_question = Retriever._passages([only_words], [found], limit=8, words=0.5).passages

        assert only_words.chunk_id in [p.chunk_id for p in as_a_name]
        assert as_a_name[0].chunk_id == only_words.chunk_id
        assert only_words.chunk_id not in [p.chunk_id for p in as_a_question]

    def test_what_both_found_comes_first_however_the_words_count(self) -> None:
        both = hit("found by both")
        meaning = [hit("the best by meaning"), both]
        found = _ByMeaning(space_id="emb", hits=meaning)

        for weight in (1.0, 0.5):
            passages = Retriever._passages(
                [both, hit("only words")], [found], limit=3, words=weight
            ).passages
            assert passages[0].chunk_id == both.chunk_id
            assert passages[0].matched_by == ("vector", "text")

    def test_the_score_is_a_share_of_the_best_that_could_be_had_at_that_weight(self) -> None:
        only = hit("alone")

        for weight in (1.0, 0.5):
            [passage] = Retriever._passages([only], [], limit=1, words=weight).passages
            assert passage.score == pytest.approx(1.0)

        both = hit("both")
        found = _ByMeaning(space_id="emb", hits=[both])
        [passage] = Retriever._passages([both], [found], limit=1, words=0.5).passages
        assert passage.score == pytest.approx(1.0)


class TestWhatIsRelevant:
    """A passage is relevant by how alike its meaning is to the query, and not by how high it came
    (ADR 0028): the score of a passage says the second, and a question about nothing still has a
    first passage."""

    def test_nothing_is_left_out_unless_a_floor_is_asked_for(self) -> None:
        close, far = hit("close", score=0.9), hit("far", score=0.1)

        ranked = Retriever._passages([], [_ByMeaning(space_id="emb", hits=[close, far])], limit=8)

        assert {p.chunk_id for p in ranked.passages} == {close.chunk_id, far.chunk_id}
        assert ranked.left_out == 0
        assert {p.similarity for p in ranked.passages} == {0.9, 0.1}

    def test_a_section_that_is_not_alike_enough_is_left_out_and_counted(self) -> None:
        close, far = hit("close", score=0.9), hit("far", score=0.1)

        ranked = Retriever._passages(
            [], [_ByMeaning(space_id="emb", hits=[close, far])], limit=8, floor=0.5
        )

        assert [p.chunk_id for p in ranked.passages] == [close.chunk_id]
        assert ranked.left_out == 1

    def test_a_section_exactly_as_alike_as_the_floor_stays(self) -> None:
        edge, under = hit("edge", score=0.5), hit("under", score=0.4999)

        ranked = Retriever._passages(
            [], [_ByMeaning(space_id="emb", hits=[edge, under])], limit=8, floor=0.5
        )

        assert [p.chunk_id for p in ranked.passages] == [edge.chunk_id]

    def test_a_section_is_as_alike_as_its_closest_piece(self) -> None:
        section = uuid4()
        near = hit("near", parent=section, score=0.2)
        close = hit("close", parent=section, score=0.7)
        # The words find the piece that is not so close: it is the one that stands for the section.
        found = _ByMeaning(space_id="emb", hits=[close, near])

        ranked = Retriever._passages([near], [found], limit=8, floor=0.5)

        [passage] = ranked.passages
        assert passage.chunk_id == near.chunk_id
        assert passage.similarity == pytest.approx(0.7)
        assert ranked.left_out == 0

    def test_what_only_the_words_found_is_not_relevant(self) -> None:
        by_words = hit("a section that has the words in it")

        ranked = Retriever._passages([by_words], [], limit=8, floor=0.1)

        assert (ranked.passages, ranked.left_out) == ((), 1)

    def test_unless_the_query_names_a_code_as_written_which_the_words_find_and_a_vector_blurs(
        self,
    ) -> None:
        by_words = hit("E-4021 means that the door is open")

        ranked = Retriever._passages([by_words], [], limit=8, floor=0.9, code=True)

        [passage] = ranked.passages
        assert (passage.chunk_id, passage.similarity, passage.matched_by) == (
            by_words.chunk_id,
            None,
            ("text",),
        )

    def test_a_code_does_not_make_relevant_what_only_the_meaning_found(self) -> None:
        far = hit("far", score=0.1)

        ranked = Retriever._passages(
            [], [_ByMeaning(space_id="emb", hits=[far])], limit=8, floor=0.5, code=True
        )

        assert (ranked.passages, ranked.left_out) == ((), 1)

    def test_a_section_that_is_left_out_does_not_take_the_place_of_one_that_is_kept(self) -> None:
        far, close = hit("far", score=0.1), hit("close", score=0.9)
        # The words put the far one first, and there is a place for one.
        found = _ByMeaning(space_id="emb", hits=[close, far])

        ranked = Retriever._passages([far], [found], limit=1, floor=0.5)

        assert [p.chunk_id for p in ranked.passages] == [close.chunk_id]
        assert ranked.left_out == 1

    def test_what_is_left_out_is_counted_whatever_the_places(self) -> None:
        close = [hit(f"close {n}", score=0.9) for n in range(3)]
        far = [hit(f"far {n}", score=0.1) for n in range(2)]

        ranked = Retriever._passages(
            [], [_ByMeaning(space_id="emb", hits=[*close, *far])], limit=1, floor=0.5
        )

        assert len(ranked.passages) == 1
        assert ranked.left_out == 2

    def test_a_similarity_a_hair_over_one_is_still_a_cosine(self) -> None:
        exact = hit("exact", score=1.0000000002)

        [passage] = Retriever._passages(
            [], [_ByMeaning(space_id="emb", hits=[exact])], limit=1
        ).passages

        assert passage.similarity == 1.0

    def test_a_passage_that_only_the_words_found_says_it_has_no_similarity(self) -> None:
        [passage] = Retriever._passages([hit("words")], [], limit=1).passages

        assert passage.similarity is None

    @pytest.mark.parametrize(("query", "is_a_code"), [("E-4021", True), ("file.txt", True)])
    def test_a_code_is_one_as_the_words_weigh_it(self, query: str, is_a_code: bool) -> None:
        assert names_a_code(query) is is_a_code
        assert words_weight(query) == 1.0

    @pytest.mark.parametrize("query", ["router", "reset the router.", "what does it cover"])
    def test_a_name_or_a_question_is_not_a_code(self, query: str) -> None:
        assert names_a_code(query) is False

    async def test_a_question_about_nothing_that_is_there_finds_nothing_relevant(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, _ = shop

        result = await harness.service.search(alice, search("submarine", min_similarity=0.4))

        assert result.passages == ()
        assert result.left_out == 3

    async def test_a_question_about_what_is_there_finds_it_and_leaves_out_the_rest(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, _ = shop

        result = await harness.service.search(alice, search("reset the router", min_similarity=0.4))

        [only] = result.passages
        assert only.text == ROUTER
        assert only.similarity == pytest.approx(0.631, abs=0.01)
        assert result.left_out == 2

    async def test_without_a_floor_the_question_about_nothing_still_has_a_first_passage(
        self, alice: Principal, shop: tuple[Harness, Collection]
    ) -> None:
        harness, _ = shop

        result = await harness.service.search(alice, search("submarine"))

        assert result.passages[0].score == pytest.approx(0.5)  # it only came by meaning
        assert result.passages[0].similarity is not None
        assert result.passages[0].similarity < 0.2
        assert result.left_out == 0

    async def test_a_floor_does_not_show_a_tenant_what_is_in_another(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        collection = await harness.collection(alice, "Manuals")
        await harness.add_text(alice, collection, "Router", ROUTER, language="en")
        await harness.process()
        stranger = make_user_principal(roles=ADMIN)  # another tenant

        mine = await harness.service.search(alice, search("reset the router", min_similarity=0.4))
        theirs = await harness.service.search(
            stranger, search("reset the router", min_similarity=0.0)
        )

        assert [p.text for p in mine.passages] == [ROUTER]
        assert (theirs.passages, theirs.left_out) == ((), 0)

    async def test_a_floor_over_what_the_words_alone_found_leaves_it_out(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        collection = await harness.collection(alice, "Manuals")
        # Every word of the query is in it, lost among a hundred others: the words find it, and its
        # meaning (as this embedder sees it) is far from the query's.
        filler = " ".join(f"w{n}" for n in range(100))
        await harness.add_text(alice, collection, "Hours", f"che ore sono adesso {filler}")
        await harness.process()
        query = "che ore sono adesso"

        found = await harness.service.search(alice, search(query))
        kept = await harness.service.search(alice, search(query, min_similarity=0.5))

        assert found.passages[0].matched_by == ("vector", "text")
        assert found.passages[0].similarity == pytest.approx(0.32, abs=0.1)
        assert (kept.passages, kept.left_out) == ((), 1)

    async def test_a_code_as_written_is_found_by_its_words_whatever_the_floor(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        collection = await harness.collection(alice, "Manuals")
        await harness.add_text(alice, collection, "Codes", "Error E-4021 means the door is open.")
        await harness.add_text(alice, collection, "Cats", CATS)
        await harness.process()

        coded = await harness.service.search(alice, search("E-4021", min_similarity=0.95))
        a_question = await harness.service.search(
            alice, search("what does the door error mean", min_similarity=0.95)
        )

        assert [p.document_title for p in coded.passages] == ["Codes"]
        assert coded.passages[0].matched_by == ("vector", "text")
        assert a_question.passages == ()

    def test_the_floor_is_between_nothing_and_everything(self) -> None:
        for bad in (-0.1, 1.1):
            with pytest.raises(ValueError, match="min_similarity"):
                search("router", min_similarity=bad)


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


class TestHowMuchEachSearchLooksAt:
    @pytest.fixture
    async def asked(
        self, alice: Principal, shop: tuple[Harness, Collection], monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, list[int]]:
        harness, _ = shop
        limits: dict[str, list[int]] = {"text": [], "vector": []}
        search_text, search_vector = harness.store.search_text, harness.store.search_vector

        async def spy_text(*args: object, limit: int, **more: object) -> list[Hit]:
            limits["text"].append(limit)
            return await search_text(*args, limit=limit, **more)  # type: ignore[arg-type]

        async def spy_vector(*args: object, limit: int, **more: object) -> list[Hit]:
            limits["vector"].append(limit)
            return await search_vector(*args, limit=limit, **more)  # type: ignore[arg-type]

        monkeypatch.setattr(harness.store, "search_text", spy_text)
        monkeypatch.setattr(harness.store, "search_vector", spy_vector)
        return limits

    @pytest.mark.parametrize(
        ("wanted", "looked_at"),
        [(1, 40), (5, 40), (6, 48), (10, 80), (25, 200), (50, 200)],
    )
    async def test_both_searches_look_at_several_pieces_for_each_passage_wanted(
        self,
        alice: Principal,
        shop: tuple[Harness, Collection],
        asked: dict[str, list[int]],
        wanted: int,
        looked_at: int,
    ) -> None:
        harness, _ = shop

        await harness.service.search(alice, search("router reset", limit=wanted))

        assert asked == {"text": [looked_at], "vector": [looked_at]}


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
