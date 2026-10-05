"""A client that takes the voice of an answer as a stream (ADR 0023): it asks for it, plays what it
is sent as it arrives, stops it on the spot when the person speaks over it, and says what the edge
does that the protocol does not allow."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from factotum.client_core import AudioStream
from factotum.client_core.testing import (
    ClientRig,
    FakeMarkedSink,
    FakeSpeaker,
    FakeVoiceOutput,
    FakeVoiceSink,
    MemoryConnection,
    RecordingCommands,
)
from factotum.contracts.audio import AudioPayload
from factotum.contracts.commands import CommandResult, DeviceCommand, SetVolume
from factotum.contracts.edge import (
    CommandFrame,
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


def words(seq: int, text: str = "Hold the button.", utterance: str = "u1") -> PartFrame:
    return PartFrame(utterance_id=utterance, seq=seq, part=Speech(text=text))


def start(rate: int = 22050, utterance: str = "u1") -> VoiceStart:
    return VoiceStart(utterance_id=utterance, sample_rate=rate)


def chunk(seq: int, data: bytes, part_seq: int = 0, utterance: str = "u1") -> VoiceChunk:
    return VoiceChunk(utterance_id=utterance, seq=seq, part_seq=part_seq, data=data)


def end(utterance: str = "u1") -> VoiceEnd:
    return VoiceEnd(utterance_id=utterance)


def over(status: TurnStatus = TurnStatus.FINISHED, utterance: str = "u1") -> TurnEnded:
    return TurnEnded(utterance_id=utterance, status=status)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def streaming(
    sink: FakeVoiceSink | None = None, *, voice_output: FakeVoiceOutput | None = None
) -> tuple[ClientRig, "asyncio.Task[Any]", str, FakeVoiceSink]:
    """A speaker that takes a voice as a stream, told that it will be sent one, that has said
    something and awaits the answer."""
    sink = sink or FakeVoiceSink()
    rig = ClientRig("speaker", voice_sink=sink, voice_output=voice_output)
    running = await rig.running(features=STREAMING)
    identity = await rig.client.say("how do I reset the router?")
    return rig, running, identity, sink


class TestAskingForAVoice:
    async def test_a_speaker_with_a_sink_says_it_takes_the_voice_as_a_stream(self) -> None:
        rig = ClientRig("speaker", voice_sink=FakeVoiceSink())
        rig.connection.say(rig.ready(features=STREAMING))

        await rig.client.connect()

        [hello] = rig.connection.frames("hello")
        assert hello.features == STREAMING
        assert rig.client.features == STREAMING
        await rig.client.close()

    async def test_a_device_with_no_speaker_asks_for_none_whatever_it_has_to_play_with(
        self,
    ) -> None:
        rig = ClientRig("screen", voice_sink=FakeVoiceSink())
        rig.connection.say(rig.ready())

        await rig.client.connect()

        [hello] = rig.connection.frames("hello")
        assert hello.features == frozenset()
        await rig.client.close()

    async def test_a_device_with_a_speaker_that_plays_clips_only_asks_for_none(self) -> None:
        rig = ClientRig("speaker", voice_output=FakeVoiceOutput())
        rig.connection.say(rig.ready())

        await rig.client.connect()

        [hello] = rig.connection.frames("hello")
        assert hello.features == frozenset()
        await rig.client.close()

    async def test_an_edge_that_does_not_do_it_is_not_a_bug_and_clips_are_played_as_before(
        self,
    ) -> None:
        speaker = FakeSpeaker()
        rig = ClientRig("speaker", voice_sink=speaker, voice_output=speaker)
        running = await rig.running()  # ready, with no features
        identity = await rig.client.say("hello")

        rig.connection.say(
            PartFrame(utterance_id=identity, seq=0, part=Speech(text="Hi.", audio=RECORDING))
        )
        rig.connection.say(over())
        await rig.client.wait_for_turn(identity)

        assert speaker.played == [RECORDING]
        assert speaker.calls == []  # no track was given
        assert rig.shown.of("violation") == []
        running.cancel()

    async def test_an_edge_that_will_do_what_was_not_asked_for_is_a_bug(self) -> None:
        rig = ClientRig("speaker")  # no sink: it asked for nothing

        await rig.connected(features=STREAMING)

        assert rig.shown.of("violation") == [
            "the edge will do what this device did not ask for: voice_stream"
        ]
        await rig.client.close()


class TestPlayingTheVoiceOfAnAnswer:
    async def test_a_track_is_given_to_the_speaker_as_it_arrives_and_ends_with_the_turn(
        self,
    ) -> None:
        rig, running, identity, sink = await streaming()

        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00\x02\x00"))
        rig.connection.say(chunk(1, b"\x03\x00"))
        rig.connection.say(end())
        rig.connection.say(over())
        ended = await rig.client.wait_for_turn(identity)
        await asyncio.wait_for(rig.client.wait_until_played(), 1)

        assert ended is not None
        [track] = sink.tracks
        assert (track.sample_rate, bytes(track.pcm), track.finished) == (
            22050,
            b"\x01\x00\x02\x00\x03\x00",
            True,
        )
        assert rig.shown.of("speech") == ["Hold the button."]  # and the words are shown as ever
        assert rig.shown.of("violation") == []
        running.cancel()

    async def test_the_sound_of_two_sentences_is_one_track(self) -> None:
        rig, running, identity, sink = await streaming()

        rig.connection.say(words(0, "One sentence."))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00", part_seq=0))
        rig.connection.say(words(1, "Another sentence."))
        rig.connection.say(chunk(1, b"\x02\x00", part_seq=1))
        rig.connection.say(end())
        rig.connection.say(over())
        await rig.client.wait_for_turn(identity)
        await asyncio.wait_for(rig.client.wait_until_played(), 1)

        assert [bytes(t.pcm) for t in sink.tracks] == [b"\x01\x00\x02\x00"]
        assert rig.shown.of("violation") == []
        running.cancel()

    async def test_a_speaker_that_wants_to_know_is_told_which_sentence_each_chunk_is_of(
        self,
    ) -> None:
        sink = FakeMarkedSink()
        rig, running, identity, _ = await streaming(sink)

        rig.connection.say(words(0, "One sentence."))
        rig.connection.say(words(1, "Another sentence."))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00", part_seq=0))
        rig.connection.say(chunk(1, b"\x02\x00", part_seq=0))
        rig.connection.say(chunk(2, b"\x03\x00", part_seq=1))
        rig.connection.say(end())
        rig.connection.say(over())
        await rig.client.wait_for_turn(identity)
        await asyncio.wait_for(rig.client.wait_until_played(), 1)

        [track] = sink.tracks
        assert [(part, bytes(pcm)) for part, pcm in track.sentences] == [
            (0, b"\x01\x00\x02\x00"),
            (1, b"\x03\x00"),
        ]
        running.cancel()

    async def test_a_voice_that_changes_its_rate_is_a_track_after_a_track(self) -> None:
        rig, running, identity, sink = await streaming()

        rig.connection.say(words(0))
        rig.connection.say(words(1, "More."))
        rig.connection.say(start(22050))
        rig.connection.say(chunk(0, b"\x01\x00", part_seq=0))
        rig.connection.say(end())
        rig.connection.say(start(16000))
        rig.connection.say(chunk(0, b"\x02\x00", part_seq=1))
        rig.connection.say(end())
        rig.connection.say(over())
        await rig.client.wait_for_turn(identity)
        await asyncio.wait_for(rig.client.wait_until_played(), 1)

        assert [(t.sample_rate, bytes(t.pcm)) for t in sink.tracks] == [
            (22050, b"\x01\x00"),
            (16000, b"\x02\x00"),
        ]
        assert rig.shown.of("violation") == []
        running.cancel()

    async def test_what_is_received_is_not_waited_for_to_be_played(self) -> None:
        gate = asyncio.Event()
        rig, running, _identity, sink = await streaming(FakeVoiceSink(gate=gate))
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await asyncio.wait_for(sink.writing.wait(), 1)  # the speaker is busy with it

        rig.connection.say(CommandFrame(id="c1", command=SetVolume(level=40)))
        [ack] = await rig.connection.wait_for("ack")  # and the client is not

        assert ack.id == "c1"
        assert rig.client.playing
        gate.set()
        running.cancel()

    async def test_a_turn_that_failed_has_what_it_sent_played_to_its_end(self) -> None:
        rig, running, identity, sink = await streaming()

        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FAILED, code="x"))
        await rig.client.wait_for_turn(identity)
        await asyncio.wait_for(rig.client.wait_until_played(), 1)

        [track] = sink.tracks
        assert (bytes(track.pcm), track.finished) == (b"\x01\x00", True)
        assert (
            rig.shown.of("violation") == []
        )  # a turn that failed has no end of its sound to wait for
        running.cancel()

    async def test_a_sound_that_is_over_is_not_playing(self) -> None:
        rig, running, identity, _sink = await streaming()
        assert not rig.client.playing

        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        rig.connection.say(end())
        rig.connection.say(over())
        await rig.client.wait_for_turn(identity)
        await asyncio.wait_for(rig.client.wait_until_played(), 1)

        assert not rig.client.playing
        running.cancel()

    async def test_the_sound_stops_when_the_connection_is_lost(self) -> None:
        gate = asyncio.Event()
        rig, running, _identity, sink = await streaming(FakeVoiceSink(gate=gate))
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await asyncio.wait_for(sink.writing.wait(), 1)

        rig.connection.drop()
        lost = await running

        assert lost is not None
        assert "abort" in sink.calls
        assert not rig.client.playing

    async def test_the_sound_stops_when_the_client_is_closed(self) -> None:
        gate = asyncio.Event()
        rig, running, _identity, sink = await streaming(FakeVoiceSink(gate=gate))
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await asyncio.wait_for(sink.writing.wait(), 1)

        await rig.client.close()
        await running

        assert "abort" in sink.calls

    async def test_closing_the_client_stops_the_sound_without_waiting_for_what_reads(self) -> None:
        class Stuck(RecordingCommands):
            async def execute(self, command: DeviceCommand) -> CommandResult:
                await asyncio.Event().wait()  # hands that do not come back
                return CommandResult.ok()

        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink, hands=Stuck())
        running = await rig.running(features=STREAMING)
        await rig.client.say("how do I reset the router?")
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        rig.connection.say(CommandFrame(id="c1", command=SetVolume(level=40)))  # holds up the loop
        await settle()
        assert sink.calls == ["begin", "write"]  # it is playing, and what reads is held up

        await rig.client.close()

        assert "abort" in sink.calls
        running.cancel()

    async def test_a_speaker_that_fails_is_said_and_the_answer_goes_on(self) -> None:
        sink = FakeVoiceSink()
        sink.fails["begin"] = OSError("the speaker is in use")
        rig, running, identity, _ = await streaming(sink)

        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        rig.connection.say(end())
        rig.connection.say(over())
        ended = await rig.client.wait_for_turn(identity)
        await asyncio.wait_for(rig.client.wait_until_played(), 1)

        assert ended is not None
        assert (
            rig.shown.of("notice")[-1]
            == "the speaker could not play the sound: the speaker is in use"
        )
        assert rig.shown.of("speech") == ["Hold the button."]
        running.cancel()


class RecordingConnection(MemoryConnection):
    """A connection that writes what is sent into a list that a speaker writes into as well, so
    that the order in which a client does things can be seen."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def send(self, text: str) -> None:
        await super().send(text)
        self._events.append(f"sent {self.frames()[-1].type}")


class RecordingSink(FakeVoiceSink):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def abort(self) -> None:
        self._events.append("the speaker was stopped")
        await super().abort()


async def pieces_of(*pieces: bytes) -> AsyncGenerator[bytes]:
    for piece in pieces:
        yield piece


class TestSpeakingOverTheAnswer:
    async def test_what_is_typed_stops_the_sound_before_the_edge_is_told_of_it(self) -> None:
        events: list[str] = []
        sink = RecordingSink(events)
        rig = ClientRig("speaker", voice_sink=sink, connection=RecordingConnection(events))
        running = await rig.running(features=STREAMING)
        identity = await rig.client.say("one")
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await settle()
        events.clear()

        await rig.client.say("two")

        assert events == ["the speaker was stopped", "sent utterance"]
        assert identity == "u1"
        running.cancel()

    async def test_a_person_who_starts_to_talk_stops_the_sound_before_audio_start_is_sent(
        self,
    ) -> None:
        events: list[str] = []
        sink = RecordingSink(events)
        rig = ClientRig("speaker", voice_sink=sink, connection=RecordingConnection(events))
        running = await rig.running(features=STREAMING)
        await rig.client.say("one")
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await settle()
        events.clear()

        await rig.client.say_audio_stream(AudioStream(16000, pieces_of(b"\x01\x00")))

        assert events[:2] == ["the speaker was stopped", "sent audio_start"]
        running.cancel()

    async def test_a_recording_that_is_said_whole_stops_it_too(self) -> None:
        rig, running, _identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await settle()

        await rig.client.say_audio(RECORDING)

        assert "abort" in sink.calls
        running.cancel()

    async def test_a_cancel_stops_it_and_says_so(self) -> None:
        rig, running, identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await settle()

        assert await rig.client.cancel() is True

        assert "abort" in sink.calls
        assert [c.id for c in rig.connection.frames("cancel")] == [identity]
        running.cancel()

    async def test_what_is_still_on_its_way_for_an_answer_that_was_cancelled_is_let_go_of_too(
        self,
    ) -> None:
        rig, running, identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await settle()
        await rig.client.cancel()
        calls_before = list(sink.calls)

        rig.connection.say(chunk(1, b"\x02\x00"))  # in flight when the person cancelled
        rig.connection.say(end())
        rig.connection.say(over(TurnStatus.CANCELLED))
        await rig.client.wait_for_turn(identity)

        assert sink.calls == calls_before  # none of it was played
        assert rig.shown.of("violation") == []  # and it is nobody's bug
        running.cancel()

    async def test_what_is_still_on_its_way_for_the_answer_that_was_left_is_let_go_of_in_silence(
        self,
    ) -> None:
        rig, running, identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())
        await settle()
        await rig.client.say("two")  # the person speaks over it: u1 is left
        calls_before = list(sink.calls)

        rig.connection.say(chunk(0, b"\x01\x00"))  # in flight when the person spoke
        rig.connection.say(chunk(1, b"\x02\x00"))
        rig.connection.say(end())
        rig.connection.say(over(TurnStatus.CANCELLED))
        await rig.client.wait_for_turn(identity)

        assert sink.calls == calls_before  # none of it was played
        assert rig.shown.of("violation") == []  # and it is nobody's bug
        running.cancel()

    async def test_the_answer_to_what_was_said_instead_is_played(self) -> None:
        rig, running, _identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())
        await settle()
        second = await rig.client.say("two")

        rig.connection.say(over(TurnStatus.CANCELLED))
        rig.connection.say(words(0, "The answer.", second))
        rig.connection.say(start(utterance=second))
        rig.connection.say(chunk(0, b"\x07\x00", utterance=second))
        rig.connection.say(end(second))
        rig.connection.say(over(utterance=second))
        await rig.client.wait_for_turn(second)
        await asyncio.wait_for(rig.client.wait_until_played(), 1)

        assert bytes(sink.tracks[-1].pcm) == b"\x07\x00"
        assert rig.shown.of("violation") == []
        running.cancel()


class TestWhenTheEdgeIsWrongAboutTheVoice:
    async def test_sound_for_a_device_that_did_not_ask_for_it(self) -> None:
        rig = ClientRig("speaker")  # no sink, no feature
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.say(start())
        await settle()

        assert rig.shown.of("violation") == [
            "the edge sent sound to a device that did not ask for it"
        ]
        assert identity == "u1"
        running.cancel()

    async def test_sound_for_an_answer_that_nobody_is_waiting_for(self) -> None:
        rig, running, _identity, sink = await streaming()

        rig.connection.say(start(utterance="never-asked"))
        await settle()

        [violation] = rig.shown.of("violation")
        assert "nobody is waiting for" in violation
        assert sink.calls == []
        running.cancel()

    async def test_sound_after_the_end_of_its_turn(self) -> None:
        rig, running, identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(over())
        await rig.client.wait_for_turn(identity)

        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        await settle()

        assert len(rig.shown.of("violation")) == 2
        assert sink.calls == []
        running.cancel()

    async def test_a_chunk_outside_a_track(self) -> None:
        rig, running, _identity, sink = await streaming()
        rig.connection.say(words(0))

        rig.connection.say(chunk(0, b"\x01\x00"))
        await settle()

        assert rig.shown.of("violation") == ["the edge sent a chunk of sound outside a track"]
        assert sink.calls == []
        running.cancel()

    async def test_a_track_that_begins_while_one_is_going_on(self) -> None:
        rig, running, _identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())

        rig.connection.say(start(16000))
        await settle()

        assert rig.shown.of("violation") == [
            "the edge began a track of sound while one was going on"
        ]
        assert sink.calls == ["begin"]  # the second was not given
        running.cancel()

    async def test_the_end_of_a_track_that_was_not_going_on(self) -> None:
        rig, running, _identity, _sink = await streaming()

        rig.connection.say(end())
        await settle()

        assert rig.shown.of("violation") == [
            "the edge ended a track of sound that was not going on"
        ]
        running.cancel()

    async def test_a_track_that_was_ended_is_ended_once(self) -> None:
        rig, running, _identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        rig.connection.say(end())

        rig.connection.say(end())
        await settle()

        assert rig.shown.of("violation") == [
            "the edge ended a track of sound that was not going on"
        ]
        assert sink.calls == ["begin", "write", "finish"]  # and it was finished once
        running.cancel()

    async def test_a_chunk_after_its_track_ended(self) -> None:
        rig, running, _identity, sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))
        rig.connection.say(end())

        rig.connection.say(chunk(1, b"\x02\x00"))
        await settle()

        assert rig.shown.of("violation") == ["the edge sent a chunk of sound outside a track"]
        assert sink.sound == b"\x01\x00"  # the one that came too late was not played
        running.cancel()

    async def test_chunks_that_come_out_of_order(self) -> None:
        rig, running, _identity, _sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())

        rig.connection.say(chunk(0, b"\x01\x00"))
        rig.connection.say(chunk(2, b"\x03\x00"))
        await settle()

        [violation] = rig.shown.of("violation")
        assert "out of order" in violation
        assert "1 was expected, 2 came" in violation
        running.cancel()

    async def test_the_sound_of_a_part_that_is_not_a_speech_part_that_was_sent(self) -> None:
        rig, running, _identity, _sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())

        rig.connection.say(chunk(0, b"\x01\x00", part_seq=5))
        await settle()

        [violation] = rig.shown.of("violation")
        assert "part 5" in violation
        running.cancel()

    async def test_a_turn_that_finished_with_its_sound_not_ended(self) -> None:
        rig, running, identity, _sink = await streaming()
        rig.connection.say(words(0))
        rig.connection.say(start())
        rig.connection.say(chunk(0, b"\x01\x00"))

        rig.connection.say(over())
        await rig.client.wait_for_turn(identity)

        assert rig.shown.of("violation") == ["an answer ended with its sound not ended"]
        running.cancel()

    async def test_a_clip_in_a_speech_part_to_a_device_that_takes_its_voice_as_a_stream(
        self,
    ) -> None:
        speaker = FakeSpeaker()
        rig = ClientRig("speaker", voice_sink=speaker, voice_output=speaker)
        running = await rig.running(features=STREAMING)
        identity = await rig.client.say("hello")

        rig.connection.say(
            PartFrame(utterance_id=identity, seq=0, part=Speech(text="Hi.", audio=RECORDING))
        )
        rig.connection.say(over())
        await rig.client.wait_for_turn(identity)

        assert rig.shown.of("violation") == [
            "the edge sent a clip in a speech part to a device that takes its voice as a stream"
        ]
        assert speaker.played == []  # it was not played: it is what the edge was not to send
        running.cancel()
