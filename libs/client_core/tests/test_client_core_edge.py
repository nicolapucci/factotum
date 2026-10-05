"""The client against a real edge: uvicorn serving the edge on a socket, the client of the core on
the other side, and a conversation service and a voice that do as they are told. What the two say
to each other is what a device and an edge would say, so this is where the two halves of the voice
of an answer (ADR 0023) are shown to fit: the sound arrives as the voice makes it, a person who
speaks over it stops it on both sides, and a device that did not ask for a voice is sent clips.
(The tests of the client alone are the other files here, those of the edge alone are in
``services/edge``.)"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from factotum.client_core import (
    CommandRouter,
    ConnectionLost,
    DeviceClient,
    VoiceOutput,
    VoiceSink,
    WebSocketConnection,
)
from factotum.client_core.testing import (
    FakeVoiceOutput,
    FakeVoiceSink,
    RecordingCommands,
    RecordingRenderer,
    a_device,
)
from factotum.contracts.capabilities import Capability
from factotum.contracts.edge import TurnEnded, TurnStatus
from factotum.contracts.speech import SynthesisFormat, SynthesisSamples
from factotum.edge.testing import (
    DEFAULT_CLIP,
    HANG,
    KITCHEN_CREDENTIAL,
    FakeSpeech,
    Rig,
    Serving,
    answer,
    forgotten,
    serving,
)

FIRST = "Hold the reset button for ten seconds."
SECOND = "Then let it go and wait two minutes."


@dataclass
class Speaker:
    client: DeviceClient
    shown: RecordingRenderer
    listening: "asyncio.Task[ConnectionLost | None]"


@asynccontextmanager
async def a_speaker(
    edge: Serving, *, sink: VoiceSink | None = None, output: VoiceOutput | None = None
) -> AsyncIterator[Speaker]:
    """A speaker of the kitchen, connected to the edge and told it is ready."""
    identity = a_device(
        frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT}),
        hardware_id="kitchen-01",
        token=KITCHEN_CREDENTIAL,
    )
    shown = RecordingRenderer()
    client = DeviceClient(
        await WebSocketConnection.open(edge.websocket_url),
        identity,
        shown,
        CommandRouter(RecordingCommands()),
        voice_sink=sink,
        voice_output=output,
    )
    await client.connect()
    listening = asyncio.create_task(client.run())
    try:
        yield Speaker(client, shown, listening)
    finally:
        await client.close()
        await asyncio.wait({listening})


async def eventually(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    for _ in range(round(timeout_s / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it did not happen in time")


def heard(sink: FakeVoiceSink, pcm: bytes) -> Callable[[], bool]:
    """Whether the first track that the speaker was given is what the voice made so far."""
    return lambda: bool(sink.tracks) and bytes(sink.tracks[0].pcm) == pcm


def sound(pcm: bytes, rate: int = 22050) -> list[SynthesisFormat | SynthesisSamples]:
    return [SynthesisFormat(sample_rate=rate), SynthesisSamples(data=pcm)]


class TestTheVoiceOfAnAnswer:
    async def test_the_sound_reaches_the_speaker_as_the_voice_makes_it(self) -> None:
        gate = asyncio.Event()
        speech = FakeSpeech().will_stream(
            SynthesisFormat(sample_rate=22050),
            SynthesisSamples(data=b"AAAA"),
            gate,  # the voice makes no more until the test says so
            SynthesisSamples(data=b"BBBB"),
        )
        sink = FakeVoiceSink()
        async with Rig(speech=speech) as rig, serving(rig) as edge, a_speaker(edge, sink=sink) as s:
            rig.conversation.scripts.append(answer(FIRST))

            identity = await s.client.say("how do I reset the router?")
            await eventually(heard(sink, b"AAAA"))
            assert s.client.playing  # the first of it is being played, and the rest is to come
            assert s.shown.of("speech") == [FIRST]  # and the words are on the screen

            gate.set()
            ended = await asyncio.wait_for(s.client.wait_for_turn(identity), 2)
            await asyncio.wait_for(s.client.wait_until_played(), 2)

            assert ended is not None
            assert ended.status is TurnStatus.FINISHED
            assert [(t.sample_rate, bytes(t.pcm), t.finished) for t in sink.tracks] == [
                (22050, b"AAAABBBB", True)
            ]
            assert s.shown.of("violation") == []  # and the edge did nothing it should not

    async def test_every_sentence_of_an_answer_is_one_sound_in_the_order_of_the_answer(
        self,
    ) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA")).will_stream(*sound(b"BBBB"))
        sink = FakeVoiceSink()
        async with Rig(speech=speech) as rig, serving(rig) as edge, a_speaker(edge, sink=sink) as s:
            rig.conversation.scripts.append(answer(FIRST + " ", SECOND))

            identity = await s.client.say("how do I reset the router?")
            await asyncio.wait_for(s.client.wait_for_turn(identity), 2)
            await asyncio.wait_for(s.client.wait_until_played(), 2)

            assert s.shown.of("speech") == [FIRST, SECOND]
            assert [bytes(t.pcm) for t in sink.tracks] == [b"AAAABBBB"]
            assert s.shown.of("violation") == []

    async def test_a_speaker_that_plays_clips_only_is_sent_clips_and_plays_them(self) -> None:
        speech = FakeSpeech()
        output = FakeVoiceOutput()
        async with (
            Rig(speech=speech) as rig,
            serving(rig) as edge,
            a_speaker(edge, output=output) as s,
        ):
            rig.conversation.scripts.append(answer(FIRST))

            identity = await s.client.say("how do I reset the router?")
            await asyncio.wait_for(s.client.wait_for_turn(identity), 2)

            assert output.played == [DEFAULT_CLIP]
            assert speech.voices == []  # no sound was asked for as a stream
            assert s.shown.of("violation") == []


class TestSpeakingOverTheVoice:
    async def test_the_person_stops_it_where_they_are_and_the_edge_stops_making_it(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA"), HANG).will_stream(*sound(b"CCCC"))
        sink = FakeVoiceSink()
        async with Rig(speech=speech) as rig, serving(rig) as edge, a_speaker(edge, sink=sink) as s:
            rig.conversation.scripts.append(answer(FIRST + " ", SECOND))
            rig.conversation.scripts.append(answer(FIRST))
            first = await s.client.say("how do I reset the router?")
            await eventually(heard(sink, b"AAAA"))

            second = await s.client.say("and what about the button?")  # speaking over it
            cut = await asyncio.wait_for(s.client.wait_for_turn(first), 2)
            ended = await asyncio.wait_for(s.client.wait_for_turn(second), 2)
            await asyncio.wait_for(s.client.wait_until_played(), 2)

            assert cut is not None
            assert cut.status is TurnStatus.CANCELLED
            assert ended is not None
            assert ended.status is TurnStatus.FINISHED
            assert sink.tracks[0].aborted  # what was playing was cut at once
            assert bytes(sink.tracks[-1].pcm) == b"CCCC"  # and the answer to the second is heard
            assert speech.endings[0] == "cancelled"  # and the voice that was making the first
            assert speech.open_voices == 0
            assert s.shown.of("violation") == []  # the sound that was in flight is nobody's bug

    async def test_a_cancel_stops_the_sound_on_both_sides(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA"), HANG)
        sink = FakeVoiceSink()
        async with Rig(speech=speech) as rig, serving(rig) as edge, a_speaker(edge, sink=sink) as s:
            rig.conversation.scripts.append(answer(FIRST))
            identity = await s.client.say("how do I reset the router?")
            await eventually(heard(sink, b"AAAA"))

            assert await s.client.cancel() is True
            ended = await asyncio.wait_for(s.client.wait_for_turn(identity), 2)

            assert isinstance(ended, TurnEnded)
            assert ended.status is TurnStatus.CANCELLED
            assert sink.tracks[0].aborted
            await eventually(lambda: speech.open_voices == 0)
            assert s.shown.of("violation") == []


async def test_a_client_that_closes_is_forgotten_by_the_edge_with_its_sound() -> None:
    speech = FakeSpeech().will_stream(*sound(b"AAAA"), HANG)
    sink = FakeVoiceSink()
    async with Rig(speech=speech) as rig, serving(rig) as edge:
        async with a_speaker(edge, sink=sink) as s:
            rig.conversation.scripts.append(answer(FIRST))
            await s.client.say("how do I reset the router?")
            await eventually(lambda: bool(sink.tracks))

        assert await forgotten(rig)
        assert speech.open_voices == 0
