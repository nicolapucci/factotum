"""The stand-in of the chat model can be made to go quiet in the middle of an answer, which is when
a person speaks over it, and it says when it was let go of before it was done."""

import asyncio

from factotum.contracts.chat import ChatMessage, ChatRequest, EventUnion, MessageRole, TextDelta
from factotum.e2e.models import HouseholdModel, answer_from

PROMPT = (
    "=== SOURCES abc ===\n[1] Manual\nTo reset the router, hold the reset button for ten seconds."
    "\n=== END SOURCES abc ===\n\nMessage: How do I reset the router?"
)
"""What an answer is asked, and so what it is: a sentence of the manual, quoted, and a closing."""

WRITTEN = answer_from(PROMPT)


def asked() -> ChatRequest:
    return ChatRequest(messages=(ChatMessage(role=MessageRole.USER, content=PROMPT),), stream=True)


async def read_to_the_end(model: HouseholdModel) -> list[EventUnion]:
    return [event async for event in model.chat_stream(None, None, asked())]  # type: ignore[arg-type]


def words_of(events: list[EventUnion]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


class TestAnAnswerThatIsNotHeld:
    async def test_it_is_written_whole_a_few_words_at_a_time_and_nobody_let_go_of_it(self) -> None:
        model = HouseholdModel()

        events = await read_to_the_end(model)

        assert words_of(events) == WRITTEN
        assert len([e for e in events if isinstance(e, TextDelta)]) == -(-len(WRITTEN) // 16)
        assert (model.held.is_set(), model.abandoned) == (False, 0)

    async def test_a_hold_that_is_further_than_the_answer_goes_never_comes(self) -> None:
        model = HouseholdModel()
        model.hold_after = 1000

        events = await read_to_the_end(model)

        assert words_of(events) == WRITTEN
        assert model.held.is_set() is False


class TestAnAnswerThatGoesQuiet:
    async def test_it_waits_where_it_was_told_to_and_goes_on_when_it_is_let(self) -> None:
        model = HouseholdModel()
        model.hold_after = 3
        events: list[EventUnion] = []

        async def read() -> None:
            async for event in model.chat_stream(None, None, asked()):  # type: ignore[arg-type]
                events.append(event)  # noqa: PERF401 - looked at while it is held, not after

        reading = asyncio.create_task(read())
        await asyncio.wait_for(model.held.wait(), 1)
        await asyncio.sleep(0.01)

        assert not reading.done()
        assert words_of(events) == WRITTEN[: 3 * 16]  # three pieces, and the rest is kept back
        model.release.set()
        await asyncio.wait_for(reading, 1)
        assert words_of(events) == WRITTEN
        assert model.abandoned == 0

    async def test_one_that_is_given_up_on_while_it_is_quiet_says_it_was_let_go_of(self) -> None:
        model = HouseholdModel()
        model.hold_after = 1

        async def read() -> None:
            async for _ in model.chat_stream(None, None, asked()):  # type: ignore[arg-type]
                pass

        reading = asyncio.create_task(read())
        await asyncio.wait_for(model.held.wait(), 1)

        reading.cancel()
        await asyncio.wait({reading})

        assert reading.cancelled()
        assert model.abandoned == 1

    async def test_so_is_one_that_is_closed_in_the_middle_of_being_written(self) -> None:
        model = HouseholdModel()
        stream = model.chat_stream(None, None, asked())  # type: ignore[arg-type]

        await anext(stream)
        await stream.aclose()

        assert model.abandoned == 1

    async def test_one_whose_words_were_all_written_is_not_let_go_of_by_being_closed_after(
        self,
    ) -> None:
        model = HouseholdModel()
        stream = model.chat_stream(None, None, asked())  # type: ignore[arg-type]
        words = ""
        async for event in stream:
            if isinstance(event, TextDelta):
                words += event.text
            else:
                break  # the first thing that is not a word: the account of what it cost

        await stream.aclose()

        assert words == WRITTEN
        assert model.abandoned == 0
