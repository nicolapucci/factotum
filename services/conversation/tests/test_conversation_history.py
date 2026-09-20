import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from factotum.contracts.conversation import Period, WarningCode
from factotum.contracts.errors import (
    ContextTooLongError,
    EmbeddingNotConfiguredError,
    GatewayUnavailableError,
)
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import ExchangeId, TenantId
from factotum.conversation.history import EMBED_CHARS, History, Keeper, Recall
from factotum.conversation.store import InMemoryStore
from factotum.conversation.testing import Clock, WordEmbedder
from factotum.model_client import Embedded

TENANT = TenantId(uuid4())
OWNER = Owner(kind=OwnerKind.USER, id=uuid4())
GAP = timedelta(minutes=30)


class AliasEmbedder(WordEmbedder):
    """A model that knows two words mean the same, which words alone cannot."""

    def vector(self, text: str) -> tuple[float, ...]:
        return super().vector(text.replace("reset", "restore"))


class Rig:
    def __init__(self, embedder: WordEmbedder | None = None) -> None:
        self.store = InMemoryStore()
        self.clock = Clock()
        self.embedder = embedder or WordEmbedder()
        self.history = History(
            self.store, self.embedder, zone=ZoneInfo("Europe/Rome"), now=self.clock
        )

    async def say(
        self,
        question: str,
        answer: str = "Fine.",
        *,
        owner: Owner = OWNER,
        ago: timedelta = timedelta(),
    ) -> ExchangeId:
        at = self.clock() - ago
        conversation = await self.store.open_conversation(
            TENANT, owner, device_id=None, conversation_id=None, now=at, session_gap=GAP
        )
        exchange = await self.store.add_exchange(
            TENANT,
            owner,
            conversation_id=conversation.id,
            exchange_id=ExchangeId(uuid4()),
            question=question,
            answer=answer,
            citations=(),
            untrusted=False,
            now=at,
        )
        return exchange.id

    async def search(
        self,
        query: str | None = None,
        when: Period | None = None,
        *,
        limit: int = 3,
        owner: Owner = OWNER,
    ) -> Recall:
        return await self.history.search(TENANT, owner, query=query, when=when, limit=limit)


class TestSearchingByTimeAlone:
    async def test_what_was_said_in_the_period_comes_latest_first_with_nothing_to_go_by(
        self,
    ) -> None:
        rig = Rig()
        await rig.say("two days ago", ago=timedelta(days=2))
        await rig.say("yesterday morning", ago=timedelta(days=1, hours=1))
        await rig.say("yesterday noon", ago=timedelta(days=1, hours=-1))
        await rig.say("today")

        found = await rig.search(when=Period.YESTERDAY)

        assert [h.exchange.question for h in found.hits] == ["yesterday noon", "yesterday morning"]
        assert all(h.score == 1.0 and h.matched_by == () for h in found.hits)
        assert found.warnings == ()

    async def test_a_day_is_the_day_where_the_person_is(self) -> None:
        rig = Rig()
        rig.clock.advance(hours=13)  # 22:30 UTC on the 21st: half past midnight in Rome on the 22nd
        await rig.say("just now")
        await rig.say("this afternoon", ago=timedelta(hours=8))  # 14:30 UTC: the 21st in Rome

        today = await rig.search(when=Period.TODAY)
        yesterday = await rig.search(when=Period.YESTERDAY)

        assert [h.exchange.question for h in today.hits] == ["just now"]
        assert [h.exchange.question for h in yesterday.hits] == ["this afternoon"]


class TestSearchingByWordsAndMeaning:
    async def test_what_both_find_comes_first_and_says_how_it_was_found(self) -> None:
        rig = Rig()
        await rig.say("how do I reset the router?", "Hold the button.")
        await rig.say("what is a good pasta?", "Try carbonara.")
        await rig.history.embed_pending()

        found = await rig.search("reset the router")

        best = found.hits[0]
        assert best.exchange.question == "how do I reset the router?"
        assert best.matched_by == ("vector", "text")
        assert best.score == pytest.approx(1.0)
        assert all(0 < h.score <= 1 for h in found.hits)

    async def test_what_only_the_meaning_finds_is_found_too(self) -> None:
        rig = Rig(AliasEmbedder())
        await rig.say("how do I restore the factory settings?", "Hold the button.")
        await rig.history.embed_pending()

        found = await rig.search("reset")

        assert [h.exchange.question for h in found.hits] == [
            "how do I restore the factory settings?"
        ]
        assert found.hits[0].matched_by == ("vector",)

    async def test_what_only_the_words_find_is_found_before_it_has_a_vector(self) -> None:
        rig = Rig()
        await rig.say("the router again")  # said a moment ago: its vector is not made yet

        found = await rig.search("router")

        assert [h.matched_by for h in found.hits] == [("text",)]
        assert found.warnings == ()

    async def test_how_many_come_back_is_bounded(self) -> None:
        rig = Rig()
        for n in range(5):
            await rig.say(f"router {n}")

        found = await rig.search("router", limit=2)

        assert len(found.hits) == 2

    async def test_it_looks_in_the_period_asked_for_only(self) -> None:
        rig = Rig()
        await rig.say("router last week", ago=timedelta(days=3))
        await rig.say("router today")

        found = await rig.search("router", Period.LAST_WEEK)

        assert [h.exchange.question for h in found.hits] == ["router last week"]

    async def test_it_looks_in_what_the_owner_said_only(self) -> None:
        rig = Rig()
        await rig.say("my router")
        await rig.say("their router", owner=Owner(kind=OwnerKind.USER, id=uuid4()))
        await rig.history.embed_pending()

        found = await rig.search("router")

        assert [h.exchange.question for h in found.hits] == ["my router"]

    async def test_when_nothing_matches_the_period_is_what_there_is_to_go_by(self) -> None:
        rig = Rig()
        await rig.say("a chat about pasta", ago=timedelta(days=1, hours=1))

        with_a_period = await rig.search("submarine", Period.YESTERDAY)
        without = await rig.search("submarine")

        assert [h.exchange.question for h in with_a_period.hits] == ["a chat about pasta"]
        assert without.hits == ()


class TestWhenTheMeaningCannotBeUsed:
    async def test_no_embedding_model_leaves_the_words_and_says_so(self) -> None:
        rig = Rig()
        rig.embedder.failures.append(EmbeddingNotConfiguredError("none"))
        await rig.say("the router")

        found = await rig.search("router")

        assert [h.matched_by for h in found.hits] == [("text",)]
        [warning] = found.warnings
        assert warning.code is WarningCode.HISTORY_WORDS_ONLY
        assert "no embedding model" in warning.message

    async def test_a_model_that_does_not_answer_leaves_the_words_and_says_so(self) -> None:
        rig = Rig()
        rig.embedder.failures.append(GatewayUnavailableError("down"))
        await rig.say("the router")

        found = await rig.search("router")

        assert [h.matched_by for h in found.hits] == [("text",)]
        assert "did not answer" in found.warnings[0].message

    async def test_vectors_of_another_model_are_forgotten_and_made_again(self) -> None:
        rig = Rig()
        await rig.say("the router")
        await rig.history.embed_pending()
        rig.embedder.space_id = "emb-new"

        first = await rig.search("router")  # the old vector is no use in the new space

        assert [h.matched_by for h in first.hits] == [("text",)]
        assert await rig.history.embed_pending() == 1  # it was queued again
        second = await rig.search("router")
        assert [h.matched_by for h in second.hits] == [("vector", "text")]


class TestGivingWhatWasSaidItsVector:
    async def test_the_question_and_the_answer_are_embedded_for_their_owner(self) -> None:
        rig = Rig()
        await rig.say("how do I reset the router?", "Hold the button.")

        assert await rig.history.embed_pending() == 1

        [(owner, texts)] = rig.embedder.calls
        assert owner == OWNER
        assert texts == ["how do I reset the router?\nHold the button."]
        assert await rig.history.embed_pending() == 0  # nothing left to do

    async def test_each_owner_is_asked_for_separately(self) -> None:
        rig = Rig()
        other = Owner(kind=OwnerKind.TENANT, id=uuid4())
        await rig.say("mine")
        await rig.say("theirs", owner=other)

        assert await rig.history.embed_pending() == 2

        assert {owner for owner, _ in rig.embedder.calls} == {OWNER, other}

    async def test_only_the_start_of_a_long_exchange_is_embedded(self) -> None:
        rig = Rig()
        await rig.say("q", "word " * 2000)

        await rig.history.embed_pending()

        assert len(rig.embedder.calls[0][1][0]) == EMBED_CHARS

    async def test_nothing_pending_asks_nobody(self) -> None:
        rig = Rig()

        assert await rig.history.embed_pending() == 0
        assert rig.embedder.calls == []

    async def test_an_owner_without_an_embedding_model_is_left_for_a_quarter_of_an_hour(
        self,
    ) -> None:
        rig = Rig()
        rig.embedder.failures.append(EmbeddingNotConfiguredError("none"))
        await rig.say("q")

        assert await rig.history.embed_pending() == 0
        assert await rig.history.embed_pending() == 0  # not asked again at once
        assert len(rig.embedder.calls) == 1
        rig.clock.advance(minutes=16)
        assert await rig.history.embed_pending() == 1

    async def test_a_model_that_does_not_answer_is_tried_again_soon(self) -> None:
        rig = Rig()
        rig.embedder.failures.append(GatewayUnavailableError("down"))
        await rig.say("q")

        assert await rig.history.embed_pending() == 0
        rig.clock.advance(minutes=3)
        assert await rig.history.embed_pending() == 1

    async def test_a_text_the_model_will_not_take_is_cut_in_half_until_it_does(self) -> None:
        class Small(WordEmbedder):
            async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
                if any(len(text) > 700 for text in texts):
                    self.calls.append((owner, list(texts)))
                    raise ContextTooLongError("too long")
                return await super().embed(owner, texts)

        rig = Rig(Small())
        await rig.say("q", "word " * 2000)

        assert await rig.history.embed_pending() == 1

        assert [len(texts[0]) for _, texts in rig.embedder.calls] == [2000, 1000, 500]

    async def test_a_text_that_does_not_fit_even_small_is_left_for_a_day(self) -> None:
        class Never(WordEmbedder):
            async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
                self.calls.append((owner, list(texts)))
                raise ContextTooLongError("too long")

        rig = Rig(Never())
        await rig.say("q", "word " * 2000)

        assert await rig.history.embed_pending() == 0
        calls = len(rig.embedder.calls)
        rig.clock.advance(hours=23)
        await rig.history.embed_pending()
        assert len(rig.embedder.calls) == calls


class TestTheWorkInTheBackground:
    async def test_a_round_gives_what_is_waiting_its_vector(self) -> None:
        rig = Rig()
        await rig.say("q")
        keeper = Keeper(rig.history, rig.store, now=rig.clock)

        assert await keeper.once() == 1

    async def test_what_is_older_than_the_retention_is_forgotten_once_an_hour(self) -> None:
        rig = Rig()
        await rig.say("ancient", ago=timedelta(days=40))
        await rig.say("fresh")
        keeper = Keeper(rig.history, rig.store, retention=timedelta(days=30), now=rig.clock)

        await keeper.once()

        assert [h.exchange.question for h in (await rig.search(when=Period.THIS_MONTH)).hits] == [
            "fresh"
        ]
        assert await rig.store.has_exchanges(TENANT, OWNER)
        other = Owner(kind=OwnerKind.USER, id=uuid4())
        await rig.say("ancient again", owner=other, ago=timedelta(days=40))
        await keeper.once()  # within the hour: not looked at again
        assert await rig.store.has_exchanges(TENANT, other)
        rig.clock.advance(hours=2)
        await keeper.once()
        assert not await rig.store.has_exchanges(TENANT, other)

    async def test_without_a_retention_nothing_is_forgotten(self) -> None:
        rig = Rig()
        await rig.say("ancient", ago=timedelta(days=4000))
        keeper = Keeper(rig.history, rig.store, now=rig.clock)

        await keeper.once()

        assert len(await rig.store.list_conversations(TENANT, OWNER, limit=9, offset=0)) == 1

    async def test_it_works_until_it_is_told_to_stop_and_a_failed_round_does_not_end_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Signalling(WordEmbedder):
            enough = asyncio.Event()

            async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
                try:
                    return await super().embed(owner, texts)
                finally:
                    if len(self.calls) >= 2:
                        self.enough.set()

        embedder = Signalling()
        rig = Rig(embedder)
        embedder.failures.append(RuntimeError("a bug"))
        await rig.say("q")
        keeper = Keeper(rig.history, rig.store, interval_s=0.01, now=rig.clock)
        stop = asyncio.Event()

        with caplog.at_level(logging.ERROR, logger="factotum.conversation"):
            working = asyncio.create_task(keeper.run(stop))
            async with asyncio.timeout(5):
                await embedder.enough.wait()
            stop.set()
            await working

        assert "the history could not be kept" in caplog.text
        assert await rig.history.embed_pending() == 0  # the second round embedded it
