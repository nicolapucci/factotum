"""A person who speaks over the answer, end to end: the moment they begin, the answer stops, all the
way back to the model that was writing it, and what they say is heard and answered (ADR 0022).

What stands for a person here is a stream that begins, takes its time, and ends: a recording
cannot interrupt anything before it is over. So these tests run the stream way only. The stand-in
model writes a little of its answer and goes quiet, which is when somebody speaks over it.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

from factotum.client_core import AudioStream
from factotum.contracts.audio import pcm_of
from factotum.contracts.edge import TurnStatus
from factotum.contracts.knowledge import CollectionOwner
from factotum.e2e import devices
from factotum.e2e.devices import Carrier, connected, until
from factotum.e2e.house import running
from factotum.e2e.models import ROUTER, SPOKEN_ANSWER
from factotum.e2e.stores import Store
from factotum.e2e.voice import recording_of

QUESTION = "How do I reset the router?"
FOLLOW_UP = "What is the reset button for?"


@pytest.fixture(autouse=True)
def _how_the_house_is_run(monkeypatch: pytest.MonkeyPatch) -> Store:
    """In memory, with what a person says streamed: it is not what the stores do that is looked at
    (this is the fixture of ``conftest.py`` by the same name, which would run each test three
    times)."""
    monkeypatch.setattr(devices, "DEFAULT_CARRIER", Carrier.STREAM)
    return Store.MEMORY


class TestSpeakingOverTheAnswer:
    async def test_the_answer_stops_when_the_person_begins_and_the_model_with_it(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            house.model.hold_after = 2
            async with connected(house, "speaker") as speaker:
                first = await speaker.hands_over(recording_of(QUESTION))
                await asyncio.wait_for(
                    house.model.held.wait(), house.timeout_s
                )  # it has gone quiet
                house.model.hold_after = None  # what comes next is not held
                pcm, rate = pcm_of(recording_of(FOLLOW_UP).data)
                half = len(pcm) // 4 * 2
                gate = asyncio.Event()

                async def a_person_who_takes_a_while() -> AsyncGenerator[bytes]:
                    yield pcm[:half]
                    await gate.wait()
                    yield pcm[half:]

                saying = asyncio.create_task(
                    speaker.client.say_audio_stream(AudioStream(rate, a_person_who_takes_a_while()))
                )

                cut = await speaker.answered(first)

                # The answer stopped with the person in the middle of what they were saying, and
                # the model that was writing it, which had been left waiting, is let go of.
                assert cut.status is TurnStatus.CANCELLED
                assert not saying.done()
                assert house.whisper.heard == [QUESTION]  # the rest has not been listened to
                await until(lambda: house.model.abandoned == 1)
                gate.set()
                second = await asyncio.wait_for(saying, house.timeout_s)
                ended = await speaker.answered(second)
                assert ended.status is TurnStatus.FINISHED, ended
                assert house.whisper.heard == [QUESTION, FOLLOW_UP]
                assert speaker.understood() == [QUESTION, FOLLOW_UP]
                assert speaker.spoken() == SPOKEN_ANSWER  # the answer to the first never came
                assert speaker.shown.of("violation") == []

    async def test_an_answer_that_is_not_spoken_over_is_not_cut(self) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                answered = await speaker.says_aloud(QUESTION)
                again = await speaker.says_aloud(FOLLOW_UP)

                assert (answered.status, again.status) == (TurnStatus.FINISHED, TurnStatus.FINISHED)
                assert speaker.understood() == [QUESTION, FOLLOW_UP]
                assert house.model.abandoned == 0  # nothing was interrupted

    async def test_a_person_who_begins_and_goes_quiet_is_told_and_may_begin_again(self) -> None:
        async with running(stream_stall_s=0.2) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                goes_quiet = asyncio.Event()

                async def a_person_who_goes_quiet() -> AsyncGenerator[bytes]:
                    yield b"\x01\x00" * 800
                    await goes_quiet.wait()  # and is not heard from again, until the test is over

                quiet = asyncio.create_task(
                    speaker.client.say_audio_stream(AudioStream(16000, a_person_who_goes_quiet()))
                )
                await until(lambda: speaker.client.active is not None)
                identity = speaker.client.active
                assert identity is not None

                ended = await speaker.answered(identity)

                assert (ended.status, ended.code) == (TurnStatus.FAILED, "stream_stalled")
                assert house.whisper.heard == []  # nothing that was said was listened to
                goes_quiet.set()
                await asyncio.wait_for(quiet, house.timeout_s)
                answered = await speaker.says_aloud(QUESTION)  # and the next one is
                assert answered.status is TurnStatus.FINISHED, answered
                assert speaker.understood() == [QUESTION]
