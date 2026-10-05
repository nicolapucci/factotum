"""A person talking to a device, as the device hears them: a stream of audio (ADR 0022).

What is looked at is what the edge does with ``audio_start``, ``audio_chunk`` and ``audio_end``: it
interrupts the answer the moment a person starts to talk, puts what it was sent in a clip when they
stop and answers it as it would an utterance, says what it was taken for, and gives up on a stream
that is too big, in the wrong order, stalled or superseded, saying how, once.
"""

import asyncio
import logging
from collections.abc import Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from factotum.contracts.audio import (
    MAX_CHUNK_BYTES,
    MAX_STREAM_BYTES,
    AudioPayload,
    pcm_of,
    wav_of,
)
from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability
from factotum.contracts.conversation import MAX_MESSAGE_CHARS
from factotum.contracts.edge import (
    AudioChunk,
    AudioEnd,
    AudioStart,
    AudioUtterance,
    Cancel,
    CloseCode,
    Ping,
    TurnStatus,
    Utterance,
)
from factotum.contracts.errors import SttNotConfiguredError
from factotum.edge.testing import (
    HANG,
    USER,
    FakeSpeech,
    FakeWire,
    Rig,
    Step,
    answer,
    make_device,
    quick_settings,
)

QUESTION = "how do I reset the router?"
LONG_ENOUGH = "Hold the reset button for ten seconds. "


def going_on() -> list[Step]:
    """An answer that has said a sentence and is still being written."""
    return [*answer(LONG_ENOUGH + "Then")[:-1], HANG]


def stream_of(identity: str, *chunks: bytes, rate: int = 16000, end: bool = True) -> list[Contract]:
    """The frames of a person talking: it starts, a chunk for each of ``chunks``, and it ends."""
    frames: list[Contract] = [AudioStart(id=identity, sample_rate=rate)]
    frames += [AudioChunk(id=identity, seq=n, data=data) for n, data in enumerate(chunks)]
    if end:
        frames.append(AudioEnd(id=identity))
    return frames


def say_all(wire: FakeWire, *frames: Contract) -> None:
    for frame in frames:
        wire.say(frame)


async def until(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():  # noqa: ASYNC110 - a state to watch, there is no event
            await asyncio.sleep(0.005)


async def barrier(wire: FakeWire) -> None:
    """Everything the device said before this has been dealt with: a ping is answered in order."""
    before = len(wire.frames("pong"))
    wire.say(Ping())
    await wire.wait_for("pong", count=before + 1)


class TestATalkThatIsAnswered:
    async def test_it_is_turned_into_words_when_it_ends_and_answered_like_an_utterance(
        self,
    ) -> None:
        speech = FakeSpeech().will_transcribe(QUESTION)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, b"\x02\x00" * 800))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert [text for _, text in rig.conversation.turns] == [QUESTION]
            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.FINISHED)
            parts = connected.wire.frames("part")
            assert parts
            assert all(part.utterance_id == "u1" for part in parts)

    async def test_what_the_model_is_given_is_a_wav_file_of_every_sample_at_the_rate_it_began_with(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            say_all(
                connected.wire,
                *stream_of("u1", b"\x01\x00" * 100, b"\x02\x00" * 50, b"\x03\x00" * 10, rate=22050),
            )
            await connected.wire.wait_for("turn_ended")

            [(_, recording)] = speech.transcriptions
            assert recording.sample_rate == 22050
            assert pcm_of(recording.data) == (
                b"\x01\x00" * 100 + b"\x02\x00" * 50 + b"\x03\x00" * 10,
                22050,
            )

    async def test_nothing_is_listened_to_while_the_person_is_still_talking(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))
            await barrier(connected.wire)
            assert speech.transcriptions == []
            assert rig.conversation.turns == []

            connected.wire.say(AudioEnd(id="u1"))
            await connected.wire.wait_for("turn_ended")

            assert len(speech.transcriptions) == 1

    async def test_chunks_may_cut_a_sample_anywhere_and_the_samples_are_put_back_together(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            say_all(connected.wire, *stream_of("u1", b"\x01", b"\x00\x02\x00\x03", b"\x00"))
            await connected.wire.wait_for("turn_ended")

            [(_, recording)] = speech.transcriptions
            assert pcm_of(recording.data)[0] == b"\x01\x00\x02\x00\x03\x00"

    async def test_half_a_sample_at_the_end_is_not_listened_to(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            say_all(connected.wire, *stream_of("u1", b"\x01\x00\x02"))
            await connected.wire.wait_for("turn_ended")

            [(_, recording)] = speech.transcriptions
            assert pcm_of(recording.data)[0] == b"\x01\x00"

    async def test_the_stream_of_a_device_that_acts_for_a_user_is_heard_and_answered_as_that_user(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(make_device(shared=False), speech=speech) as rig:
            connected = await rig.connect()

            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 100))
            await connected.wire.wait_for("turn_ended")

            [(principal, _)] = rig.conversation.turns
            [(owner, _)] = speech.transcriptions
            assert (principal.acts_as.id, owner.id) == (USER, USER)

    async def test_several_talks_one_after_the_other_are_each_answered(self) -> None:
        speech = FakeSpeech().will_transcribe("one", "two")
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 100))
            await connected.wire.wait_for("turn_ended")
            say_all(connected.wire, *stream_of("u2", b"\x02\x00" * 100))
            ended = await connected.wire.wait_for("turn_ended", count=2)

            assert [(e.utterance_id, e.status) for e in ended] == [
                ("u1", TurnStatus.FINISHED),
                ("u2", TurnStatus.FINISHED),
            ]
            assert [text for _, text in rig.conversation.turns] == ["one", "two"]


class TestWhatWasHeard:
    @staticmethod
    def send(wire: FakeWire, how: str, recording: AudioPayload) -> None:
        if how == "a clip":
            wire.say(AudioUtterance(id="u1", audio=recording))
        else:
            pcm, rate = pcm_of(recording.data)
            say_all(wire, *stream_of("u1", pcm, rate=rate))

    @pytest.fixture
    def recording(self) -> AudioPayload:
        return wav_of(b"\x01\x00" * 800, 16000)

    @pytest.mark.parametrize("how", ["a clip", "a stream"])
    async def test_what_a_recording_was_taken_for_is_said_before_the_answer_begins(
        self, how: str, recording: AudioPayload
    ) -> None:
        speech = FakeSpeech().will_transcribe(QUESTION)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            self.send(connected.wire, how, recording)
            await connected.wire.wait_for("turn_ended")

            kinds = [f.type for f in connected.wire.frames("heard", "part", "turn_ended")]
            assert kinds[0] == "heard"
            assert kinds[-1] == "turn_ended"
            assert kinds.count("heard") == 1
            [heard] = connected.wire.frames("heard")
            assert (heard.utterance_id, heard.text) == ("u1", QUESTION)

    @pytest.mark.parametrize("how", ["a clip", "a stream"])
    async def test_what_was_heard_is_what_the_conversation_is_asked_without_the_margins(
        self, how: str, recording: AudioPayload
    ) -> None:
        speech = FakeSpeech().will_transcribe(f"  {QUESTION}  \n")
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            self.send(connected.wire, how, recording)
            await connected.wire.wait_for("turn_ended")

            [heard] = connected.wire.frames("heard")
            assert heard.text == QUESTION
            assert [text for _, text in rig.conversation.turns] == [QUESTION]

    @pytest.mark.parametrize("how", ["a clip", "a stream"])
    async def test_a_transcription_longer_than_a_message_is_still_heard_cut_to_one(
        self, how: str, recording: AudioPayload
    ) -> None:
        speech = FakeSpeech().will_transcribe("w" * (MAX_MESSAGE_CHARS + 50))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            self.send(connected.wire, how, recording)
            await connected.wire.wait_for("turn_ended")

            [heard] = connected.wire.frames("heard")
            assert len(heard.text) == MAX_MESSAGE_CHARS

    @pytest.mark.parametrize("how", ["a clip", "a stream"])
    async def test_a_recording_taken_for_nothing_is_not_heard_and_ends_its_turn(
        self, how: str, recording: AudioPayload
    ) -> None:
        speech = FakeSpeech().will_transcribe("   ")
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            self.send(connected.wire, how, recording)
            [ended] = await connected.wire.wait_for("turn_ended")

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "nothing_heard")
            assert connected.wire.frames("heard") == []
            assert rig.conversation.turns == []

    @pytest.mark.parametrize("how", ["a clip", "a stream"])
    async def test_a_recording_that_cannot_be_transcribed_is_not_heard_either(
        self, how: str, recording: AudioPayload
    ) -> None:
        speech = FakeSpeech().will_transcribe(SttNotConfiguredError("no speech-to-text model"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            self.send(connected.wire, how, recording)
            [ended] = await connected.wire.wait_for("turn_ended")

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "stt_not_configured")
            assert connected.wire.frames("heard") == []

    async def test_what_is_typed_is_not_heard_since_nobody_listened(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()

            connected.wire.say(Utterance(id="u1", text=QUESTION))
            await connected.wire.wait_for("turn_ended")

            assert connected.wire.frames("heard") == []

    async def test_a_device_that_cannot_render_anything_but_status_is_still_told_what_was_heard(
        self, recording: AudioPayload
    ) -> None:
        # What was heard is not a part of the answer: what a device can render does not filter it.
        mute = make_device(capabilities=[Capability.AUDIO_IN])
        speech = FakeSpeech().will_transcribe(QUESTION)
        async with Rig(mute, speech=speech) as rig:
            connected = await rig.connect()

            self.send(connected.wire, "a stream", recording)
            await connected.wire.wait_for("turn_ended")

            assert [h.text for h in connected.wire.frames("heard")] == [QUESTION]

    @pytest.mark.parametrize("how", ["a clip", "a stream"])
    async def test_a_device_that_cannot_be_told_what_was_heard_is_not_answered(
        self, how: str, recording: AudioPayload
    ) -> None:
        speech = FakeSpeech().will_transcribe(QUESTION)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            connected.wire.send_fails = True  # the first thing the edge sends it is what it heard

            self.send(connected.wire, how, recording)
            await until(lambda: len(speech.transcriptions) == 1)
            connected.wire.hang_up()
            await connected.task

            assert rig.conversation.turns == []
            assert len(rig.sessions) == 0


class TestSpeakingOverTheAnswer:
    async def test_an_answer_stops_the_moment_a_person_starts_to_talk_and_not_when_they_stop(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(going_on())
            connected.wire.say(Utterance(id="u1", text=QUESTION))
            await connected.wire.wait_for("part", count=2)  # it is being answered

            connected.wire.say(AudioStart(id="u2", sample_rate=16000))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.CANCELLED)
            assert rig.conversation.abandoned == 1  # the model that was writing it is not kept on
            assert speech.transcriptions == []  # and the person has not finished

    async def test_what_was_said_over_it_is_answered_when_it_ends(self) -> None:
        speech = FakeSpeech().will_transcribe("never mind, what time is it?")
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(going_on())
            connected.wire.say(Utterance(id="u1", text=QUESTION))
            await connected.wire.wait_for("part", count=2)

            connected.wire.say(AudioStart(id="u2", sample_rate=16000))
            await connected.wire.wait_for("turn_ended")
            say_all(
                connected.wire,
                AudioChunk(id="u2", seq=0, data=b"\x01\x00" * 800),
                AudioEnd(id="u2"),
            )
            ended = await connected.wire.wait_for("turn_ended", count=2)

            assert [(e.utterance_id, e.status) for e in ended] == [
                ("u1", TurnStatus.CANCELLED),
                ("u2", TurnStatus.FINISHED),
            ]
            assert [text for _, text in rig.conversation.turns][
                -1
            ] == "never mind, what time is it?"

    async def test_the_answer_to_a_stream_is_stopped_by_the_next_one_in_the_same_way(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(going_on())
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800))
            await connected.wire.wait_for("part", count=2)

            connected.wire.say(AudioStart(id="u2", sample_rate=16000))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.CANCELLED)
            assert rig.conversation.abandoned == 1

    async def test_a_stream_that_is_being_turned_into_words_is_given_up_on_and_says_so(
        self,
    ) -> None:
        speech = FakeSpeech().will_transcribe(HANG, QUESTION)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800))
            await until(lambda: len(speech.transcriptions) == 1)  # the model is listening

            connected.wire.say(AudioStart(id="u2", sample_rate=16000))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.CANCELLED)
            assert rig.conversation.turns == []  # what u1 said was never answered
            say_all(
                connected.wire,
                AudioChunk(id="u2", seq=0, data=b"\x02\x00" * 800),
                AudioEnd(id="u2"),
            )
            ended = await connected.wire.wait_for("turn_ended", count=2)
            assert ended[1].status is TurnStatus.FINISHED

    async def test_a_clip_that_is_being_turned_into_words_is_too(self) -> None:
        # It was given up on without a word before: the device waited for the end of a turn that
        # nobody was going to end.
        speech = FakeSpeech().will_transcribe(HANG)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            connected.wire.say(
                AudioUtterance(id="u1", audio=AudioPayload(sample_rate=16000, data=b"clip"))
            )
            await until(lambda: len(speech.transcriptions) == 1)

            connected.wire.say(Utterance(id="u2", text=QUESTION))
            ended = await connected.wire.wait_for("turn_ended", count=2)

            assert [(e.utterance_id, e.status) for e in ended] == [
                ("u1", TurnStatus.CANCELLED),
                ("u2", TurnStatus.FINISHED),
            ]

    async def test_starting_again_gives_up_on_the_talk_that_was_not_finished(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))

            say_all(connected.wire, *stream_of("u2", b"\x02\x00" * 400))
            ended = await connected.wire.wait_for("turn_ended", count=2)

            assert [(e.utterance_id, e.status) for e in ended] == [
                ("u1", TurnStatus.CANCELLED),
                ("u2", TurnStatus.FINISHED),
            ]
            [(_, recording)] = speech.transcriptions
            assert pcm_of(recording.data)[0] == b"\x02\x00" * 400  # none of u1 is in it

    async def test_a_late_frame_of_the_talk_that_was_given_up_on_does_not_end_the_new_one(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))
            say_all(connected.wire, *stream_of("u2", b"\x02\x00" * 400, end=False))

            say_all(
                connected.wire,
                AudioChunk(id="u1", seq=1, data=b"\x09\x00" * 10),  # late
                AudioEnd(id="u1"),  # late
            )
            errors = await connected.wire.wait_for("error", count=2)
            connected.wire.say(AudioEnd(id="u2"))
            ended = await connected.wire.wait_for("turn_ended", count=2)

            assert [(e.code, e.ref, e.fatal) for e in errors] == [
                ("unknown_stream", "u1", False),
                ("unknown_stream", "u1", False),
            ]
            assert [(e.utterance_id, e.status) for e in ended] == [
                ("u1", TurnStatus.CANCELLED),
                ("u2", TurnStatus.FINISHED),
            ]
            [(_, recording)] = speech.transcriptions
            assert pcm_of(recording.data)[0] == b"\x02\x00" * 400

    async def test_what_is_typed_in_the_middle_of_a_talk_gives_up_on_it(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))

            connected.wire.say(Utterance(id="u2", text="never mind"))
            ended = await connected.wire.wait_for("turn_ended", count=2)
            connected.wire.say(AudioEnd(id="u1"))
            [error] = await connected.wire.wait_for("error")

            assert [(e.utterance_id, e.status) for e in ended] == [
                ("u1", TurnStatus.CANCELLED),
                ("u2", TurnStatus.FINISHED),
            ]
            assert (error.code, error.ref) == ("unknown_stream", "u1")

    async def test_a_recording_sent_whole_in_the_middle_of_a_talk_gives_up_on_it(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))

            connected.wire.say(
                AudioUtterance(id="u2", audio=AudioPayload(sample_rate=16000, data=b"clip"))
            )
            ended = await connected.wire.wait_for("turn_ended", count=2)

            assert [(e.utterance_id, e.status) for e in ended] == [
                ("u1", TurnStatus.CANCELLED),
                ("u2", TurnStatus.FINISHED),
            ]

    async def test_a_cancel_ends_the_talk_that_is_going_on(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))

            connected.wire.say(Cancel(id="u1"))
            [ended] = await connected.wire.wait_for("turn_ended")
            connected.wire.say(AudioChunk(id="u1", seq=1, data=b"\x01\x00"))
            [error] = await connected.wire.wait_for("error")

            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.CANCELLED)
            assert error.code == "unknown_stream"

    async def test_a_cancel_for_something_else_leaves_the_talk_alone(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))

            connected.wire.say(Cancel(id="never-was"))
            await barrier(connected.wire)
            connected.wire.say(AudioEnd(id="u1"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert ended.status is TurnStatus.FINISHED
            assert len(speech.transcriptions) == 1


class TestWhatEndsATalkWithoutAnAnswer:
    async def test_a_chunk_that_is_too_big_ends_it_and_the_device_stays(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            connected.wire.say(AudioStart(id="u1", sample_rate=16000))

            connected.wire.say(AudioChunk(id="u1", seq=0, data=bytes(MAX_CHUNK_BYTES + 2)))
            [ended] = await connected.wire.wait_for("turn_ended")
            await barrier(connected.wire)

            assert (ended.utterance_id, ended.status, ended.code) == (
                "u1",
                TurnStatus.FAILED,
                "chunk_too_big",
            )
            assert connected.wire.closed_with is None
            assert speech.transcriptions == []

    async def test_a_chunk_of_exactly_the_size_that_is_allowed_is_taken(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            say_all(connected.wire, *stream_of("u1", bytes(MAX_CHUNK_BYTES)))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert ended.status is TurnStatus.FINISHED
            [(_, recording)] = speech.transcriptions
            assert len(pcm_of(recording.data)[0]) == MAX_CHUNK_BYTES

    @pytest.mark.parametrize(
        ("seqs", "expected"),
        [
            pytest.param((1,), "0 was expected and chunk 1", id="it did not begin at 0"),
            pytest.param((0, 2), "1 was expected and chunk 2", id="one went missing"),
            pytest.param((0, 0), "1 was expected and chunk 0", id="one came twice"),
            pytest.param((0, 1, 0), "2 was expected and chunk 0", id="it went back"),
        ],
    )
    async def test_chunks_out_of_order_end_it(self, seqs: tuple[int, ...], expected: str) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            connected.wire.say(AudioStart(id="u1", sample_rate=16000))

            for seq in seqs:
                connected.wire.say(AudioChunk(id="u1", seq=seq, data=b"\x01\x00" * 10))
            [ended] = await connected.wire.wait_for("turn_ended")
            connected.wire.say(AudioEnd(id="u1"))
            [error] = await connected.wire.wait_for("error")

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "stream_out_of_order")
            assert expected in ended.message
            assert error.code == "unknown_stream"  # the talk is over: its end is a late frame
            assert speech.transcriptions == []

    async def test_a_talk_that_is_more_than_a_clip_may_hold_is_ended_and_what_fits_is_taken(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            whole, rest = divmod(MAX_STREAM_BYTES, MAX_CHUNK_BYTES)
            fits = [bytes(MAX_CHUNK_BYTES)] * whole + [bytes(rest)]

            say_all(connected.wire, *stream_of("u1", *fits))
            [ended] = await connected.wire.wait_for("turn_ended")
            assert ended.status is TurnStatus.FINISHED  # exactly what a clip may hold

            say_all(connected.wire, *stream_of("u2", *fits, b"\x00\x00", end=False))
            ended = await connected.wire.wait_for("turn_ended", count=2)

            assert (ended[1].utterance_id, ended[1].status, ended[1].code) == (
                "u2",
                TurnStatus.FAILED,
                "audio_too_long",
            )
            assert len(speech.transcriptions) == 1  # only the first

    async def test_a_talk_that_stops_coming_is_dropped_and_the_device_is_not(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech, settings=quick_settings(stream_stall_s=0.05)) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))

            [ended] = await connected.wire.wait_for("turn_ended")
            await barrier(connected.wire)

            assert (ended.utterance_id, ended.status, ended.code) == (
                "u1",
                TurnStatus.FAILED,
                "stream_stalled",
            )
            assert connected.wire.closed_with is None
            assert speech.transcriptions == []
            # It may talk again.
            say_all(connected.wire, *stream_of("u2", b"\x02\x00" * 800))
            ended_again = await connected.wire.wait_for("turn_ended", count=2)
            assert ended_again[1].status is TurnStatus.FINISHED

    async def test_a_talk_that_never_sent_a_chunk_stalls_too(self) -> None:
        async with Rig(speech=FakeSpeech(), settings=quick_settings(stream_stall_s=0.05)) as rig:
            connected = await rig.connect()

            connected.wire.say(AudioStart(id="u1", sample_rate=16000))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert ended.code == "stream_stalled"

    async def test_a_device_that_keeps_sending_is_not_stalled(self) -> None:
        async with Rig(speech=FakeSpeech(), settings=quick_settings(stream_stall_s=0.15)) as rig:
            connected = await rig.connect()

            connected.wire.say(AudioStart(id="u1", sample_rate=16000))
            for seq in range(8):  # twice as long as the stall, a chunk at a time
                await asyncio.sleep(0.04)
                connected.wire.say(AudioChunk(id="u1", seq=seq, data=b"\x01\x00" * 100))
            connected.wire.say(AudioEnd(id="u1"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert ended.status is TurnStatus.FINISHED

    async def test_saying_it_is_there_does_not_keep_a_talk_that_has_stopped_alive(self) -> None:
        async with Rig(speech=FakeSpeech(), settings=quick_settings(stream_stall_s=0.1)) as rig:
            connected = await rig.connect()
            connected.wire.say(AudioStart(id="u1", sample_rate=16000))

            # A ping every few hundredths of a second, for as long as it takes: the talk is ended
            # while they are still coming, and not once they have stopped.
            async with asyncio.timeout(1.0):
                while not connected.wire.frames("turn_ended"):
                    connected.wire.say(Ping())
                    await asyncio.sleep(0.02)

            [ended] = connected.wire.frames("turn_ended")
            assert ended.code == "stream_stalled"
            assert len(connected.wire.frames("pong")) >= 2  # and the pings were answered

    async def test_a_talk_that_waits_longer_than_a_device_may_be_idle_ends_the_device(self) -> None:
        settings = quick_settings(idle_timeout_s=0.1, stream_stall_s=5.0)
        async with Rig(speech=FakeSpeech(), settings=settings) as rig:
            connected = await rig.connect()

            connected.wire.say(AudioStart(id="u1", sample_rate=16000))
            await connected.task

            [error] = connected.wire.frames("error")
            assert (error.code, error.fatal) == ("idle", True)
            assert connected.wire.closed_with is not None
            assert connected.wire.closed_with[0] == CloseCode.IDLE
            assert connected.wire.frames("turn_ended") == []  # there is nobody to tell

    async def test_chunks_are_the_device_saying_something_for_the_idle_timeout(self) -> None:
        settings = quick_settings(idle_timeout_s=0.12, stream_stall_s=5.0)
        async with Rig(speech=FakeSpeech(), settings=settings) as rig:
            connected = await rig.connect()

            connected.wire.say(AudioStart(id="u1", sample_rate=16000))
            for seq in range(6):  # twice as long as it may be quiet
                await asyncio.sleep(0.04)
                connected.wire.say(AudioChunk(id="u1", seq=seq, data=b"\x01\x00" * 100))
            connected.wire.say(AudioEnd(id="u1"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert ended.status is TurnStatus.FINISHED
            assert connected.wire.closed_with is None

    async def test_a_dropped_talk_is_logged_with_why_and_never_with_what_was_in_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="factotum.edge")
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()
            connected.wire.say(AudioStart(id="u1", sample_rate=16000))

            connected.wire.say(AudioChunk(id="u1", seq=5, data=b"secret-words" * 10))
            await connected.wire.wait_for("turn_ended")

        [record] = [r for r in caplog.records if r.getMessage() == "a stream was dropped"]
        assert record.levelno == logging.WARNING
        assert record.code == "stream_out_of_order"  # type: ignore[attr-defined]
        assert "secret-words" not in caplog.text


class TestWhatIsRefusedBeforeAnythingBegins:
    async def test_a_device_with_no_microphone_cannot_stream_and_stays(self) -> None:
        player = make_device(capabilities=[Capability.AUDIO_OUT])
        async with Rig(player, speech=FakeSpeech()) as rig:
            connected = await rig.connect()

            connected.wire.say(AudioStart(id="u1", sample_rate=16000))
            [error] = await connected.wire.wait_for("error")
            connected.wire.say(AudioChunk(id="u1", seq=0, data=b"\x01\x00"))
            await connected.wire.wait_for("error", count=2)
            await barrier(connected.wire)

            assert (error.code, error.fatal, error.ref) == ("capability_missing", False, "u1")
            assert connected.wire.closed_with is None
            assert connected.wire.frames("turn_ended") == []

    async def test_without_a_speech_client_it_is_refused_at_its_start_and_the_device_stays(
        self,
    ) -> None:
        async with Rig() as rig:  # no speech= given
            connected = await rig.connect()

            connected.wire.say(AudioStart(id="u1", sample_rate=16000))
            [error] = await connected.wire.wait_for("error")
            await barrier(connected.wire)

            assert (error.code, error.fatal, error.ref) == ("speech_not_configured", False, "u1")
            assert connected.wire.closed_with is None

    async def test_a_talk_that_is_refused_does_not_interrupt_the_answer_that_is_going_on(
        self,
    ) -> None:
        async with Rig() as rig:  # it cannot hear, so it cannot be spoken over by sound
            connected = await rig.connect()
            rig.conversation.scripts.append(going_on())
            connected.wire.say(Utterance(id="u1", text=QUESTION))
            await connected.wire.wait_for("part", count=2)

            connected.wire.say(AudioStart(id="u2", sample_rate=16000))
            await connected.wire.wait_for("error")
            await barrier(connected.wire)

            assert connected.wire.frames("turn_ended") == []
            assert rig.conversation.abandoned == 0
            connected.wire.say(Cancel(id="u1"))
            [ended] = await connected.wire.wait_for("turn_ended")
            assert ended.status is TurnStatus.CANCELLED

    @pytest.mark.parametrize(
        "frame",
        [
            AudioChunk(id="never-was", seq=0, data=b"\x01\x00"),
            AudioEnd(id="never-was"),
        ],
        ids=["a chunk", "an end"],
    )
    async def test_a_frame_of_a_talk_that_never_began_is_an_error_and_nothing_else(
        self, frame: Contract
    ) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()

            connected.wire.say(frame)
            [error] = await connected.wire.wait_for("error")
            await barrier(connected.wire)

            assert (error.code, error.fatal, error.ref) == ("unknown_stream", False, "never-was")
            assert connected.wire.closed_with is None
            assert connected.wire.frames("turn_ended") == []

    async def test_a_frame_for_another_talk_does_not_touch_the_one_that_is_going_on(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 100, end=False))

            say_all(
                connected.wire,
                AudioChunk(id="other", seq=0, data=b"\x09\x00" * 100),
                AudioEnd(id="other"),
            )
            errors = await connected.wire.wait_for("error", count=2)
            say_all(
                connected.wire,
                AudioChunk(id="u1", seq=1, data=b"\x02\x00" * 100),
                AudioEnd(id="u1"),
            )
            [ended] = await connected.wire.wait_for("turn_ended")

            assert [e.ref for e in errors] == ["other", "other"]
            assert ended.status is TurnStatus.FINISHED
            [(_, recording)] = speech.transcriptions
            assert pcm_of(recording.data)[0] == b"\x01\x00" * 100 + b"\x02\x00" * 100

    async def test_a_talk_that_has_been_answered_is_over_and_a_late_frame_of_it_is_an_error(
        self,
    ) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 100))
            await connected.wire.wait_for("turn_ended")

            connected.wire.say(AudioChunk(id="u1", seq=1, data=b"\x01\x00"))
            [error] = await connected.wire.wait_for("error")

            assert (error.code, error.ref) == ("unknown_stream", "u1")

    @pytest.mark.parametrize(
        "chunks",
        [pytest.param((), id="no chunk at all"), pytest.param((b"\x01",), id="one byte")],
    )
    async def test_a_talk_with_not_one_sample_in_it_is_not_sent_to_be_listened_to(
        self, chunks: tuple[bytes, ...]
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            say_all(connected.wire, *stream_of("u1", *chunks))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "nothing_heard")
            assert speech.transcriptions == []
            assert connected.wire.frames("heard") == []


class TestADeviceThatGoes:
    async def test_a_device_that_goes_in_the_middle_of_a_talk_leaves_nothing_behind(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800, end=False))

            connected.wire.hang_up()
            await connected.task

            assert speech.transcriptions == []
            assert rig.conversation.turns == []
            assert len(rig.sessions) == 0

    async def test_a_device_that_goes_while_its_talk_is_being_listened_to_takes_it_away(
        self,
    ) -> None:
        speech = FakeSpeech().will_transcribe(HANG)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", b"\x01\x00" * 800))
            await until(lambda: len(speech.transcriptions) == 1)

            connected.wire.hang_up()
            await connected.task

            assert rig.conversation.turns == []
            assert len(rig.sessions) == 0


@settings(max_examples=40, deadline=None)
@given(chunks=st.lists(st.binary(min_size=1, max_size=300), min_size=1, max_size=12))
def test_however_the_samples_are_cut_into_chunks_what_is_listened_to_is_the_same(
    chunks: list[bytes],
) -> None:
    async def talk() -> list[tuple[object, AudioPayload]]:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()
            say_all(connected.wire, *stream_of("u1", *chunks))
            await connected.wire.wait_for("turn_ended")
            return list(speech.transcriptions)

    joined = b"".join(chunks)
    whole = joined[: len(joined) - len(joined) % 2]

    heard = asyncio.run(talk())

    if whole:
        [(_, recording)] = heard
        assert pcm_of(recording.data) == (whole, 16000)
    else:
        assert heard == []
