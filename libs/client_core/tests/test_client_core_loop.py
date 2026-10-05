"""Conversing by voice: what a person says is sent as they say it, and the answer is waited for, or
spoken over, whatever the microphone is."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import pytest

from factotum.client_core import AudioStream, ConnectionLost, VoiceLoop
from factotum.client_core.testing import ClientRig, FakeVoiceInput, FakeVoiceSink
from factotum.contracts.audio import AudioPayload
from factotum.contracts.edge import (
    Feature,
    PartFrame,
    TurnEnded,
    TurnStatus,
    VoiceChunk,
    VoiceEnd,
    VoiceStart,
)
from factotum.contracts.response import Speech

RECORDING = AudioPayload(sample_rate=16000, data=b"raw-microphone-bytes")
STREAMING = frozenset({Feature.VOICE_STREAM})


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def said(*pieces: bytes) -> AudioStream:
    return AudioStream.of(b"".join(pieces) or b"\x01\x00", 16000)


def over(identity: str, status: TurnStatus = TurnStatus.FINISHED) -> TurnEnded:
    return TurnEnded(utterance_id=identity, status=status)


class Microphone:
    """A microphone that a test speaks into: what is put in with ``says`` is heard as it is said,
    and ``leaves`` is the person going away. It counts how many times it was opened."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AudioPayload | AudioStream | None] = asyncio.Queue()
        self.opened = 0

    def says(self, what: AudioPayload | AudioStream) -> None:
        self._queue.put_nowait(what)

    def leaves(self) -> None:
        self._queue.put_nowait(None)

    async def listen(self) -> AsyncIterator[AudioPayload | AudioStream]:
        self.opened += 1
        while (what := await self._queue.get()) is not None:
            yield what


class TestWaitingForTheAnswerBeforeListeningAgain:
    async def test_what_is_said_is_sent_and_the_answer_is_waited_for_before_the_next_is_heard(
        self,
    ) -> None:
        rig = ClientRig()
        microphone = FakeVoiceInput(said(), said(), ends=True)
        await rig.connected()

        serving = asyncio.create_task(VoiceLoop(microphone).serve(rig.client))
        await rig.connection.wait_for("audio_end")
        await settle()

        assert len(rig.connection.frames("audio_start")) == 1  # the second waits for the answer
        rig.connection.say(over("u1"))
        await rig.connection.wait_for("audio_end", count=2)
        rig.connection.say(over("u2"))
        lost = await asyncio.wait_for(serving, 1)

        assert lost is None  # the person left, and it was not the connection that ended
        assert [f.id for f in rig.connection.frames("audio_start")] == ["u1", "u2"]
        assert len(rig.connection.frames("bye")) == 1

    async def test_a_recording_that_is_said_whole_is_sent_whole(self) -> None:
        rig = ClientRig()
        await rig.connected()
        serving = asyncio.create_task(
            VoiceLoop(FakeVoiceInput(RECORDING, ends=True)).serve(rig.client)
        )

        [utterance] = await rig.connection.wait_for("audio_utterance")
        rig.connection.say(over(utterance.id))
        await asyncio.wait_for(serving, 1)

        assert utterance.audio == RECORDING

    async def test_the_answer_to_the_last_thing_said_is_waited_for_when_there_is_no_more(
        self,
    ) -> None:
        rig = ClientRig()
        await rig.connected()
        serving = asyncio.create_task(
            VoiceLoop(FakeVoiceInput(said(), ends=True)).serve(rig.client)
        )
        await rig.connection.wait_for("audio_end")
        await settle()

        assert not serving.done()  # the person has left, and what they said is still to be answered
        rig.connection.say(over("u1"))
        await asyncio.wait_for(serving, 1)

    async def test_it_waits_until_what_was_received_has_been_played_too(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig(voice_sink=sink)
        await rig.connected(features=STREAMING)
        serving = asyncio.create_task(
            VoiceLoop(FakeVoiceInput(said(), ends=True)).serve(rig.client)
        )
        await rig.connection.wait_for("audio_end")
        rig.connection.say(PartFrame(utterance_id="u1", seq=0, part=Speech(text="Hello there.")))
        rig.connection.say(VoiceStart(utterance_id="u1", sample_rate=22050))
        rig.connection.say(VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=b"\x01\x00"))
        rig.connection.say(over("u1"))
        await asyncio.wait_for(sink.writing.wait(), 1)
        await settle()

        assert not serving.done()  # the speaker has not finished: listening now would hear it
        gate.set()
        await asyncio.wait_for(serving, 1)
        assert sink.tracks[0].finished

    async def test_the_next_thing_is_not_heard_until_the_speaker_has_said_the_answer(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig(voice_sink=sink)
        await rig.connected(features=STREAMING)
        microphone = Microphone()
        serving = asyncio.create_task(VoiceLoop(microphone).serve(rig.client))
        microphone.says(said())
        microphone.says(said())  # a person who has more to say, and does not wait to be answered
        await rig.connection.wait_for("audio_end")
        rig.connection.say(PartFrame(utterance_id="u1", seq=0, part=Speech(text="Hello there.")))
        rig.connection.say(VoiceStart(utterance_id="u1", sample_rate=22050))
        rig.connection.say(VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=b"\x01\x00"))
        rig.connection.say(VoiceEnd(utterance_id="u1"))
        rig.connection.say(over("u1"))
        await asyncio.wait_for(sink.writing.wait(), 1)
        await settle()

        assert len(rig.connection.frames("audio_start")) == 1  # the answer is still being said
        assert sink.tracks[0].finished is False
        gate.set()
        await rig.connection.wait_for("audio_end", count=2)

        assert sink.tracks[0].finished  # and it was not cut by the next thing that was said
        microphone.leaves()
        rig.connection.say(over("u2"))
        assert await asyncio.wait_for(serving, 1) is None


class TestWhenTheConnectionGoes:
    async def test_it_is_noticed_without_waiting_for_anybody_to_say_anything(self) -> None:
        rig = ClientRig()
        await rig.connected()
        serving = asyncio.create_task(VoiceLoop(FakeVoiceInput()).serve(rig.client))
        await settle()

        rig.connection.drop(1001, "going away")
        lost = await asyncio.wait_for(serving, 1)

        assert isinstance(lost, ConnectionLost)
        assert (lost.code, lost.reason) == (1001, "going away")

    async def test_the_answer_that_never_came_is_not_waited_for_when_the_edge_goes(self) -> None:
        rig = ClientRig()
        await rig.connected()
        serving = asyncio.create_task(VoiceLoop(FakeVoiceInput(said())).serve(rig.client))
        await rig.connection.wait_for("audio_end")

        rig.connection.drop()
        lost = await asyncio.wait_for(serving, 1)

        assert lost is not None

    @pytest.mark.parametrize("what", [RECORDING, said()], ids=["a recording", "a stream"])
    async def test_a_connection_that_is_gone_when_something_is_said_ends_the_conversation(
        self, what: Any
    ) -> None:
        rig = ClientRig()
        await rig.connected()
        rig.connection.closed = True  # the way out is closed: what is said cannot be sent
        serving = asyncio.create_task(VoiceLoop(FakeVoiceInput(what)).serve(rig.client))

        lost = await asyncio.wait_for(serving, 1)

        assert isinstance(lost, ConnectionLost)
        assert rig.connection.frames("audio_utterance", "audio_start") == []

    async def test_a_recording_that_was_heard_as_it_went_is_not_sent_either(self) -> None:
        rig = ClientRig()
        await rig.connected()
        rig.connection.drop()
        microphone = Microphone()
        serving = asyncio.create_task(VoiceLoop(microphone).serve(rig.client))
        microphone.says(RECORDING)

        lost = await asyncio.wait_for(serving, 1)

        assert lost is not None
        assert rig.connection.frames("audio_utterance") == []

    async def test_what_was_heard_as_it_went_is_not_sent_and_what_was_taking_it_is_let_go_of(
        self,
    ) -> None:
        rig = ClientRig()
        await rig.connected()
        let_go = asyncio.Event()

        async def a_person() -> AsyncGenerator[bytes]:
            try:
                yield b"\x01\x00" * 100
                await asyncio.Event().wait()
            finally:
                let_go.set()

        microphone = Microphone()
        chunks = a_person()
        await anext(chunks)  # the person has begun, and the microphone is holding what they say
        rig.connection.drop()
        serving = asyncio.create_task(VoiceLoop(microphone).serve(rig.client))
        microphone.says(AudioStream(16000, chunks))
        lost = await asyncio.wait_for(serving, 1)

        assert lost is not None
        assert rig.connection.frames("audio_start") == []
        await asyncio.wait_for(let_go.wait(), 1)


class TestAMicrophoneThatOutlivesTheConnection:
    async def test_it_is_opened_once_and_the_next_connection_goes_on_listening_to_it(self) -> None:
        microphone = Microphone()
        loop = VoiceLoop(microphone, listen_while_answering=True)
        first, second = ClientRig(), ClientRig()
        await first.connected()
        serving = asyncio.create_task(loop.serve(first.client))
        await settle()
        first.connection.drop(1006)
        assert await asyncio.wait_for(serving, 1) is not None

        await second.connected()
        serving = asyncio.create_task(loop.serve(second.client))
        microphone.says(said())
        await second.connection.wait_for("audio_end")
        microphone.leaves()
        second.connection.say(over("u1"))
        assert await asyncio.wait_for(serving, 1) is None

        assert microphone.opened == 1
        assert first.connection.frames("audio_start") == []
        assert [f.id for f in second.connection.frames("audio_start")] == ["u1"]

    async def test_what_was_said_while_there_was_no_connection_is_not_said_to_the_next(
        self,
    ) -> None:
        microphone = Microphone()
        loop = VoiceLoop(microphone, listen_while_answering=True)
        first, second = ClientRig(), ClientRig()
        await first.connected()
        first.connection.drop(1006)
        serving = asyncio.create_task(loop.serve(first.client))
        microphone.says(said())  # heard in the moment it went
        assert await asyncio.wait_for(serving, 1) is not None

        await second.connected()
        serving = asyncio.create_task(loop.serve(second.client))
        await settle()
        microphone.leaves()
        assert await asyncio.wait_for(serving, 1) is None

        assert second.connection.frames("audio_start") == []


class TestListeningWhileTheAnswerPlays:
    async def test_the_next_thing_said_is_heard_while_the_answer_is_still_being_given(self) -> None:
        microphone = Microphone()
        sink = FakeVoiceSink()
        rig = ClientRig(voice_sink=sink)
        await rig.connected(features=STREAMING)
        serving = asyncio.create_task(
            VoiceLoop(microphone, listen_while_answering=True).serve(rig.client)
        )
        microphone.says(said())
        await rig.connection.wait_for("audio_end")
        rig.connection.say(PartFrame(utterance_id="u1", seq=0, part=Speech(text="Hello there.")))
        rig.connection.say(VoiceStart(utterance_id="u1", sample_rate=22050))
        rig.connection.say(VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=b"\x01\x00"))
        await settle()

        microphone.says(said())  # and the answer to the first is not over
        await rig.connection.wait_for("audio_end", count=2)

        assert [f.id for f in rig.connection.frames("audio_start")] == ["u1", "u2"]
        assert "abort" in sink.calls  # the person spoke over it, and it stopped
        microphone.leaves()
        rig.connection.say(over("u1", TurnStatus.CANCELLED))
        rig.connection.say(over("u2"))
        assert await asyncio.wait_for(serving, 1) is None

    async def test_what_was_said_last_is_still_answered_when_the_person_has_left(self) -> None:
        microphone = Microphone()
        rig = ClientRig()
        await rig.connected()
        serving = asyncio.create_task(
            VoiceLoop(microphone, listen_while_answering=True).serve(rig.client)
        )
        microphone.says(said())
        await rig.connection.wait_for("audio_end")
        microphone.leaves()
        await settle()

        assert not serving.done()  # the answer to what they said is still due
        rig.connection.say(over("u1"))
        assert await asyncio.wait_for(serving, 1) is None

    async def test_what_is_left_of_the_sound_is_played_before_it_is_over(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig(voice_sink=sink)
        await rig.connected(features=STREAMING)
        microphone = Microphone()
        serving = asyncio.create_task(
            VoiceLoop(microphone, listen_while_answering=True).serve(rig.client)
        )
        microphone.says(said())
        await rig.connection.wait_for("audio_end")
        rig.connection.say(PartFrame(utterance_id="u1", seq=0, part=Speech(text="Hello there.")))
        rig.connection.say(VoiceStart(utterance_id="u1", sample_rate=22050))
        rig.connection.say(VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=b"\x01\x00"))
        rig.connection.say(VoiceEnd(utterance_id="u1"))
        rig.connection.say(over("u1"))
        await asyncio.wait_for(sink.writing.wait(), 1)
        microphone.leaves()
        await settle()

        assert not serving.done()  # the answer is over, and the speaker has not said it all
        gate.set()
        assert await asyncio.wait_for(serving, 1) is None
        assert sink.tracks[0].finished
        assert "abort" not in sink.calls  # so it was not cut when the client was closed

    async def test_it_is_a_choice_of_the_device_and_the_other_is_the_default(self) -> None:
        rig = ClientRig()
        await rig.connected()
        microphone = Microphone()
        serving = asyncio.create_task(VoiceLoop(microphone).serve(rig.client))
        microphone.says(said())
        await rig.connection.wait_for("audio_end")

        microphone.says(said())
        await settle()

        assert len(rig.connection.frames("audio_start")) == 1  # waiting for the answer
        rig.connection.drop()
        await asyncio.wait_for(serving, 1)


@pytest.mark.parametrize("what", [RECORDING, said()])
async def test_what_a_microphone_that_fails_is_told_to_whoever_asked(what: Any) -> None:
    class Broken:
        async def listen(self) -> AsyncIterator[AudioPayload | AudioStream]:
            yield what
            raise OSError("the microphone was unplugged")

    rig = ClientRig()
    await rig.connected()
    serving = asyncio.create_task(VoiceLoop(Broken()).serve(rig.client))
    await rig.connection.wait_for(
        "audio_end" if isinstance(what, AudioStream) else "audio_utterance"
    )
    rig.connection.say(over("u1"))

    with pytest.raises(OSError, match="unplugged"):
        await asyncio.wait_for(serving, 1)
    assert len(rig.connection.frames("bye")) == 1  # and the client was still said goodbye to
