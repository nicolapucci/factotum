"""A spoken answer sent as a stream of sound (ADR 0023): who is sent one, what the frames are and in
which order, what is made while the text goes on, and what stops it."""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import pytest

from factotum.contracts.capabilities import Capability
from factotum.contracts.edge import (
    AudioStart,
    Cancel,
    Feature,
    TurnStatus,
    Utterance,
    encode_frame,
)
from factotum.contracts.errors import (
    ChatNotConfiguredError,
    ProviderUnavailableError,
    TtsNotConfiguredError,
)
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.response import Speech, Text
from factotum.contracts.speech import SynthesisFormat, SynthesisSamples
from factotum.edge.ports import Disconnected
from factotum.edge.testing import (
    HANG,
    SPEAKER,
    TENANT,
    FakeSpeech,
    FakeWire,
    Rig,
    Step,
    VoiceStep,
    Wait,
    answer,
    make_device,
)
from factotum.edge.voice import Voice

STREAMING = (Feature.VOICE_STREAM,)

FIRST = "Hold the reset button for ten seconds."
SECOND = "Then let it go and wait two minutes."
THIRD = "After that the light turns green."

OWNER = Owner.of_subject(Subject.tenant(TENANT))


def two_sentences() -> list[Step]:
    """An answer of two sentences, each long enough to be a part of its own."""
    return answer(FIRST + " ", SECOND)


def sound(pcm: bytes, rate: int = 22050) -> list[VoiceStep]:
    """What a voice gives for a sentence that it makes in one piece."""
    return [SynthesisFormat(sample_rate=rate), SynthesisSamples(data=pcm)]


def kinds(wire: FakeWire) -> list[str]:
    return [frame.type for frame in wire.frames()]


def spoken(wire: FakeWire) -> list[Any]:
    """The parts of the answer that are said aloud."""
    return [f.part for f in wire.frames("part") if isinstance(f.part, Speech)]


def samples_of(wire: FakeWire) -> bytes:
    return b"".join(frame.data for frame in wire.frames("voice_chunk"))


async def eventually(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    for _ in range(round(timeout_s / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it did not happen in time")


class TestWhoIsSentAVoice:
    async def test_a_device_that_says_it_takes_one_is_told_the_edge_will_send_it(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect(features=STREAMING)

            assert connected.ready.features == frozenset(STREAMING)
            assert connected.session.features == frozenset(STREAMING)

    async def test_a_device_that_says_nothing_of_it_is_told_of_no_features_and_is_sent_clips(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            connected.wire.say(Utterance(id="u1", text="hello"))
            await connected.wire.wait_for("turn_ended")

            assert connected.ready.features == frozenset()
            assert "features" not in json.loads(encode_frame(connected.ready))
            assert not connected.wire.frames("voice_start", "voice_chunk", "voice_end")
            assert [part.audio is not None for part in spoken(connected.wire)] == [True]
            assert speech.voices == []

    async def test_a_voice_is_not_taken_by_a_device_that_is_not_registered_for_a_speaker(
        self,
    ) -> None:
        microphone = make_device(capabilities=[Capability.AUDIO_IN])
        async with Rig(microphone, speech=FakeSpeech()) as rig:
            connected = await rig.connect(
                capabilities={Capability.AUDIO_IN, Capability.AUDIO_OUT}, features=STREAMING
            )

            assert connected.ready.features == frozenset()
            assert connected.ready.unregistered == {Capability.AUDIO_OUT}


class TestTheSoundOfAnAnswer:
    async def test_the_sound_follows_the_words_in_chunks_and_ends_before_the_turn_does(
        self,
    ) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AABBCCDD"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(answer(FIRST))

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert kinds(connected.wire) == [
                "ready",
                "part",  # searching
                "part",  # the words
                "voice_start",
                "voice_chunk",
                "voice_end",
                "turn_ended",
            ]
            [start] = connected.wire.frames("voice_start")
            [chunk] = connected.wire.frames("voice_chunk")
            [end] = connected.wire.frames("voice_end")
            assert (start.utterance_id, start.sample_rate) == ("u1", 22050)
            assert (chunk.utterance_id, chunk.seq, chunk.part_seq, chunk.data) == (
                "u1",
                0,
                1,
                b"AABBCCDD",
            )
            assert end.utterance_id == "u1"
            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.FINISHED)

    async def test_the_words_carry_no_clip_since_the_sound_is_sent_apart(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            assert [part.text for part in spoken(connected.wire)] == [FIRST, SECOND]
            assert [part.audio for part in spoken(connected.wire)] == [None, None]

    async def test_the_sound_of_each_sentence_is_asked_for_by_its_words_for_whom_the_device_acts(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            assert speech.voices == [(OWNER, FIRST), (OWNER, SECOND)]
            assert speech.syntheses == []  # no clip is asked for: the sound is a stream

    async def test_every_sentence_is_in_one_track_in_the_order_of_the_answer(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA")).will_stream(*sound(b"BBBB"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            chunks = connected.wire.frames("voice_chunk")
            assert [(c.seq, c.part_seq, c.data) for c in chunks] == [
                (0, 1, b"AAAA"),
                (1, 2, b"BBBB"),
            ]
            assert len(connected.wire.frames("voice_start")) == 1
            assert len(connected.wire.frames("voice_end")) == 1
            assert samples_of(connected.wire) == b"AAAABBBB"

    async def test_the_sound_of_a_sentence_follows_its_words_and_the_end_of_it_the_last_chunk(
        self,
    ) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA")).will_stream(*sound(b"BBBB"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            frames = connected.wire.frames()
            where = {
                ("part", f.seq): n for n, f in enumerate(frames) if f.type == "part"
            }  # the words of each part
            for n, frame in enumerate(frames):
                if frame.type == "voice_chunk":
                    assert where[("part", frame.part_seq)] < n
            start = next(n for n, f in enumerate(frames) if f.type == "voice_start")
            end = next(n for n, f in enumerate(frames) if f.type == "voice_end")
            assert start < min(n for n, f in enumerate(frames) if f.type == "voice_chunk")
            assert end > max(n for n, f in enumerate(frames) if f.type == "voice_chunk")
            assert [f.type for f in frames[-2:]] == ["voice_end", "turn_ended"]

    async def test_a_sentence_that_the_voice_makes_in_pieces_is_sent_in_as_many_chunks(
        self,
    ) -> None:
        speech = FakeSpeech().will_stream(
            SynthesisFormat(sample_rate=16000),
            SynthesisSamples(data=b"AA"),
            SynthesisSamples(data=b"BB"),
            SynthesisSamples(data=b"CC"),
        )
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(answer(FIRST))

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            chunks = connected.wire.frames("voice_chunk")
            assert [(c.seq, c.data) for c in chunks] == [(0, b"AA"), (1, b"BB"), (2, b"CC")]
            assert {c.part_seq for c in chunks} == {1}

    async def test_the_voice_that_the_edge_is_given_at_another_rate_begins_another_track(
        self,
    ) -> None:
        speech = (
            FakeSpeech()
            .will_stream(*sound(b"AAAA", rate=22050))
            .will_stream(*sound(b"BBBB", rate=16000))
        )
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            voice = [
                (f.type, getattr(f, "sample_rate", None), getattr(f, "seq", None))
                for f in connected.wire.frames("voice_start", "voice_chunk", "voice_end")
            ]
            assert voice == [
                ("voice_start", 22050, None),
                ("voice_chunk", None, 0),
                ("voice_end", None, None),
                ("voice_start", 16000, None),
                ("voice_chunk", None, 0),
                ("voice_end", None, None),
            ]

    async def test_a_screen_with_a_speaker_gets_the_text_and_the_sound_of_the_same_sentence(
        self,
    ) -> None:
        speech = FakeSpeech()
        smart = make_device(capabilities=[*SPEAKER, Capability.DISPLAY])
        async with Rig(smart, speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(answer(FIRST))

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            parts = [f.part for f in connected.wire.frames("part")]
            assert [type(p) for p in parts if isinstance(p, (Speech, Text))] == [Speech, Text]
            assert speech.voices == [(OWNER, FIRST)]  # only what is said aloud is made into sound

    async def test_what_is_said_when_an_answer_cannot_be_given_is_spoken_too(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"SORRY!"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append([ChatNotConfiguredError("no chat model is set up")])

            connected.wire.say(Utterance(id="u1", text="hello"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert speech.voices == [(OWNER, "I have no model to answer with yet.")]
            assert samples_of(connected.wire) == b"SORRY!"
            assert [f.type for f in connected.wire.frames()[-2:]] == ["voice_end", "turn_ended"]
            assert (ended.status, ended.code) == (TurnStatus.FAILED, "chat_not_configured")

    async def test_a_device_that_takes_a_voice_is_sent_only_the_words_when_the_edge_has_no_speech(
        self,
    ) -> None:
        async with Rig() as rig:  # no speech= given
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(answer(FIRST))

            connected.wire.say(Utterance(id="u1", text="hello"))
            await connected.wire.wait_for("turn_ended")

            assert connected.ready.features == frozenset(STREAMING)
            assert [part.text for part in spoken(connected.wire)] == [FIRST]
            assert not connected.wire.frames("voice_start", "voice_chunk", "voice_end")

    async def test_an_answer_with_nothing_to_say_aloud_has_no_track(self) -> None:
        screen_and_mic = make_device(capabilities=[Capability.AUDIO_IN, Capability.AUDIO_OUT])
        speech = FakeSpeech()
        async with Rig(screen_and_mic, speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(answer())  # it said nothing

            connected.wire.say(Utterance(id="u1", text="hello"))
            await connected.wire.wait_for("turn_ended")

            assert not connected.wire.frames("voice_start", "voice_chunk", "voice_end")
            assert speech.voices == []


class TestTheWordsDoNotWaitForTheSound:
    async def test_the_text_goes_on_while_the_sound_of_a_sentence_is_still_being_made(self) -> None:
        speech = FakeSpeech().will_stream(SynthesisFormat(sample_rate=22050), HANG)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("part", count=3)  # searching, the first and the second
            await asyncio.sleep(0.05)

            assert connected.wire.frames("turn_ended") == []  # the sound is not all there
            assert rig.conversation.open == 0  # but all of the text was read
            assert speech.voices == [(OWNER, FIRST)]  # and the second sentence waits its turn

    async def test_a_voice_that_stalls_is_given_up_on_with_the_answer(self) -> None:
        speech = FakeSpeech().will_stream(SynthesisFormat(sample_rate=22050), HANG)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(answer(FIRST))
            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("voice_start")

            connected.wire.say(Cancel(id="u1"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert ended.status is TurnStatus.CANCELLED
            assert speech.endings == ["cancelled"]
            assert speech.open_voices == 0


class TestASentenceThatCannotBeSaid:
    async def test_it_has_no_sound_and_the_next_is_tried(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        speech = (
            FakeSpeech()
            .will_stream(ProviderUnavailableError("the voice is away"))
            .will_stream(*sound(b"BBBB"))
        )
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            with caplog.at_level(logging.WARNING, logger="factotum.edge"):
                connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
                [ended] = await connected.wire.wait_for("turn_ended")

            assert [part.text for part in spoken(connected.wire)] == [FIRST, SECOND]
            assert [(c.part_seq, c.data) for c in connected.wire.frames("voice_chunk")] == [
                (2, b"BBBB")
            ]
            assert ended.status is TurnStatus.FINISHED
            [record] = [r for r in caplog.records if "could not be spoken" in r.getMessage()]
            assert record.__dict__["code"] == "provider_unavailable"
            assert "the voice is away" not in caplog.text

    async def test_a_sentence_that_fails_after_some_of_its_sound_keeps_what_was_sent(self) -> None:
        speech = (
            FakeSpeech()
            .will_stream(*sound(b"AAAA"), ProviderUnavailableError("the voice fell over"))
            .will_stream(*sound(b"BBBB"))
        )
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert samples_of(connected.wire) == b"AAAABBBB"
            assert len(connected.wire.frames("voice_start")) == 1
            assert ended.status is TurnStatus.FINISHED

    async def test_a_failure_that_is_a_bug_is_logged_and_the_answer_goes_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        speech = (
            FakeSpeech().will_stream(RuntimeError("detail of the bug")).will_stream(*sound(b"BBBB"))
        )
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert "detail of the bug" in caplog.text  # for whoever fixes it
            assert "detail of the bug" not in "".join(connected.wire.sent)
            assert samples_of(connected.wire) == b"BBBB"
            assert ended.status is TurnStatus.FINISHED

    async def test_with_no_voice_bound_there_is_no_sound_and_it_is_not_asked_for_again(
        self,
    ) -> None:
        speech = FakeSpeech().will_stream(TtsNotConfiguredError("no text-to-speech model"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert len(speech.voices) == 1  # the second sentence was not even tried
            assert [part.text for part in spoken(connected.wire)] == [FIRST, SECOND]
            assert not connected.wire.frames("voice_start", "voice_chunk", "voice_end")
            assert ended.status is TurnStatus.FINISHED

    async def test_not_being_bound_for_a_voice_is_not_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        speech = FakeSpeech().will_stream(TtsNotConfiguredError("no text-to-speech model"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)

            with caplog.at_level(logging.INFO, logger="factotum.edge"):
                connected.wire.say(Utterance(id="u1", text="hello"))
                await connected.wire.wait_for("turn_ended")

            assert not [r for r in caplog.records if "could not be spoken" in r.getMessage()]

    async def test_voices_that_come_with_no_format_are_not_sent(self) -> None:
        speech = FakeSpeech().will_stream(SynthesisSamples(data=b"AAAA"), *sound(b"BBBB"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(answer(FIRST))

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            assert samples_of(connected.wire) == b"BBBB"


class TestInterruptingTheSound:
    async def test_a_person_who_speaks_over_the_answer_stops_its_sound_where_it_is(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA"), HANG)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())
            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("voice_chunk")

            connected.wire.say(AudioStart(id="u2", sample_rate=16000))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.CANCELLED)
            assert speech.endings == ["cancelled"]
            assert speech.open_voices == 0
            assert connected.wire.frames()[-1] == ended  # no sound follows the end of a turn
            assert not connected.wire.frames("voice_end")

    async def test_a_new_utterance_stops_the_sound_of_the_one_before_and_has_a_track_of_its_own(
        self,
    ) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA"), HANG).will_stream(*sound(b"BBBB"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())
            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("voice_chunk")

            rig.conversation.scripts.append(answer(THIRD))
            connected.wire.say(Utterance(id="u2", text="and then?"))
            await connected.wire.wait_for("turn_ended", count=2)

            frames = connected.wire.frames()
            first_end = next(
                n for n, f in enumerate(frames) if f.type == "turn_ended" and f.utterance_id == "u1"
            )
            assert not [
                f for f in frames[first_end + 1 :] if getattr(f, "utterance_id", "") == "u1"
            ]
            second = [f for f in frames if getattr(f, "utterance_id", "") == "u2"]
            assert [f.type for f in second if f.type.startswith("voice")] == [
                "voice_start",
                "voice_chunk",
                "voice_end",
            ]
            assert second[-1].type == "turn_ended"
            assert [f.data for f in second if f.type == "voice_chunk"] == [b"BBBB"]

    async def test_a_cancel_stops_the_sound(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA"), HANG)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())
            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("voice_chunk")

            connected.wire.say(Cancel(id="u1"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert ended.status is TurnStatus.CANCELLED
            assert speech.open_voices == 0
            assert connected.wire.frames()[-1] == ended

    async def test_a_device_that_goes_away_mid_sound_leaves_nothing_running(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA"), HANG)
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(two_sentences())
            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("voice_chunk")

            connected.wire.hang_up()
            await connected.task

            assert speech.open_voices == 0
            assert speech.endings == ["cancelled"]
            assert rig.conversation.open == 0
            assert len(rig.sessions) == 0

    async def test_a_device_that_cannot_be_written_to_in_the_middle_of_the_sound_is_gone(
        self,
    ) -> None:
        speech = FakeSpeech().will_stream(
            *sound(b"AAAA"), Wait(0.2), SynthesisSamples(data=b"BBBB")
        )
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=STREAMING)
            rig.conversation.scripts.append(answer(FIRST))
            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("voice_chunk")

            connected.wire.send_fails = True
            await eventually(lambda: speech.open_voices == 0)
            connected.wire.hang_up()
            await connected.task

            assert speech.endings == ["closed"]  # it stopped asking the voice for more
            assert samples_of(connected.wire) == b"AAAA"
            assert len(rig.sessions) == 0


class RecordedSend:
    """What a voice sent, in the order it sent it; ``fails_after`` frames and it is gone."""

    def __init__(self, fails_after: int | None = None) -> None:
        self.frames: list[Any] = []
        self._fails_after = fails_after

    async def __call__(self, frame: Any) -> None:
        if self._fails_after is not None and len(self.frames) >= self._fails_after:
            raise Disconnected
        self.frames.append(frame)


def voice_with(speech: FakeSpeech, send: RecordedSend) -> Voice:
    return Voice(speech=speech, owner=OWNER, utterance_id="u1", send=send, session_id="s1")


class TestTheVoiceOfAnAnswerOnItsOwn:
    async def test_with_nothing_said_nothing_is_sent_and_it_is_over_at_once(self) -> None:
        send = RecordedSend()
        voice = voice_with(FakeSpeech(), send)

        await voice.finish()

        assert send.frames == []

    async def test_what_was_said_is_sent_in_order_and_ended_when_it_is_finished(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA")).will_stream(*sound(b"BBBB"))
        send = RecordedSend()
        voice = voice_with(speech, send)

        voice.say(3, "one")
        voice.say(5, "two")
        await voice.finish()

        assert [(f.type, getattr(f, "part_seq", None)) for f in send.frames] == [
            ("voice_start", None),
            ("voice_chunk", 3),
            ("voice_chunk", 5),
            ("voice_end", None),
        ]
        assert [text for _, text in speech.voices] == ["one", "two"]

    async def test_it_is_finished_once_and_what_is_said_after_is_not_made(self) -> None:
        speech = FakeSpeech()
        send = RecordedSend()
        voice = voice_with(speech, send)
        voice.say(0, "one")

        await voice.finish()
        voice.say(1, "two")
        await voice.finish()
        await voice.abort()

        assert [text for _, text in speech.voices] == ["one"]
        assert [f.type for f in send.frames] == ["voice_start", "voice_chunk", "voice_end"]

    async def test_aborting_gives_up_on_the_sentence_being_made_and_sends_no_end(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA"), HANG)
        send = RecordedSend()
        voice = voice_with(speech, send)
        voice.say(0, "one")
        voice.say(1, "two")
        await eventually(lambda: len(send.frames) == 2)

        await voice.abort()
        voice.say(2, "three")
        await voice.abort()

        assert [f.type for f in send.frames] == ["voice_start", "voice_chunk"]
        assert speech.endings == ["cancelled"]
        assert [text for _, text in speech.voices] == ["one"]

    async def test_a_device_that_is_gone_ends_the_voice_without_an_error(self) -> None:
        speech = FakeSpeech().will_stream(*sound(b"AAAA")).will_stream(*sound(b"BBBB"))
        send = RecordedSend(fails_after=1)  # the track opens, and the first chunk is refused
        voice = voice_with(speech, send)
        voice.say(0, "one")
        voice.say(1, "two")

        await voice.finish()

        assert [f.type for f in send.frames] == ["voice_start"]
        assert [text for _, text in speech.voices] == ["one"]  # the second was never asked for
