"""The embedder the tests of everything above the gateway depend on: it must behave like a model
in the ways those tests rely on, or they prove nothing."""

import math
from uuid import uuid4

import pytest

from factotum.contracts.errors import ContextTooLongError, GatewayUnavailableError
from factotum.contracts.identity import Owner, OwnerKind
from factotum.knowledge.testing import FakeEmbedder

OWNER = Owner(kind=OwnerKind.USER, id=uuid4())


def similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class TestVectors:
    def test_a_vector_has_the_size_of_the_space_and_a_length_of_one(self) -> None:
        vector = FakeEmbedder(dimensions=32).vector("the router boots")

        assert len(vector) == 32
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0)

    def test_the_same_text_is_always_the_same_vector(self) -> None:
        assert FakeEmbedder().vector("the router boots") == FakeEmbedder().vector(
            "the router boots"
        )

    def test_texts_that_share_words_are_nearer_than_texts_that_do_not(self) -> None:
        embedder = FakeEmbedder()
        near = similarity(
            embedder.vector("reset the router"), embedder.vector("router reset button")
        )
        far = similarity(embedder.vector("reset the router"), embedder.vector("cats sleep all day"))

        assert near > far

    def test_capitals_and_accents_do_not_matter(self) -> None:
        embedder = FakeEmbedder()

        assert embedder.vector("La CITTÀ") == embedder.vector("la citta")

    def test_a_text_with_no_words_is_still_a_unit_vector(self) -> None:
        vector = FakeEmbedder().vector("?! ... ---")

        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0)

    def test_words_that_mean_the_same_are_the_same_direction(self) -> None:
        embedder = FakeEmbedder()
        embedder.aliases = {"car": "automobile"}

        assert embedder.vector("car") == embedder.vector("automobile")


class TestBehaviour:
    async def test_it_answers_with_a_vector_for_each_text_and_says_where_they_live(self) -> None:
        embedder = FakeEmbedder(space_id="emb-x", dimensions=8)

        result = await embedder.embed(OWNER, ["one", "two"])

        assert (result.space_id, result.dimensions, len(result.vectors)) == ("emb-x", 8, 2)

    async def test_it_keeps_what_it_was_asked_and_for_whom(self) -> None:
        embedder = FakeEmbedder()

        await embedder.embed(OWNER, ["one", "two"])

        assert embedder.calls == [(OWNER, ["one", "two"])]

    async def test_a_failure_is_raised_once_and_the_call_after_it_goes_through(self) -> None:
        embedder = FakeEmbedder()
        embedder.failures = [GatewayUnavailableError("down")]

        with pytest.raises(GatewayUnavailableError):
            await embedder.embed(OWNER, ["one"])

        assert (await embedder.embed(OWNER, ["one"])).vectors

    async def test_a_none_lets_one_call_through_before_the_failure(self) -> None:
        embedder = FakeEmbedder()
        embedder.failures = [None, GatewayUnavailableError("down")]

        await embedder.embed(OWNER, ["one"])
        with pytest.raises(GatewayUnavailableError):
            await embedder.embed(OWNER, ["two"])

    async def test_a_text_too_long_for_the_model_is_refused(self) -> None:
        embedder = FakeEmbedder()
        embedder.max_chars = 5

        with pytest.raises(ContextTooLongError):
            await embedder.embed(OWNER, ["short", "much too long"])
        assert (await embedder.embed(OWNER, ["short"])).vectors

    async def test_something_can_happen_while_a_call_is_being_answered(self) -> None:
        embedder = FakeEmbedder()
        seen: list[int] = []

        async def during() -> None:
            seen.append(len(embedder.calls))

        embedder.during = during

        await embedder.embed(OWNER, ["one"])

        assert seen == [1]
