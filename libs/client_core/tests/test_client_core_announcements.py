"""A command that carries a clip (ADR 0026): a device with a speaker takes it at once, says it when
nothing else is being said and in the order it came, and a person who speaks cuts what is said.

What is looked at is the order of what the speaker was given and when the edge was answered: the
sound itself is a fake that remembers it."""

import asyncio
from typing import Any

import pytest

from factotum.client_core import client as client_module
from factotum.client_core.testing import (
    ClientRig,
    FakeSpeaker,
    FakeVoiceOutput,
    FakeVoiceSink,
    RecordingCommands,
)
from factotum.contracts.audio import AudioPayload, wav_of
from factotum.contracts.commands import CommandStatus, PlayAudio, SetVolume
from factotum.contracts.edge import (
    CommandFrame,
    Feature,
    PartFrame,
    Pong,
    TurnEnded,
    TurnStatus,
    VoiceChunk,
    VoiceEnd,
    VoiceStart,
)
from factotum.contracts.response import Speech

PLAYING = frozenset({Feature.PLAY_AUDIO})
STREAMING = frozenset({Feature.VOICE_STREAM, Feature.PLAY_AUDIO})


def clip(fill: int = 1, samples: int = 100, rate: int = 22_050) -> AudioPayload:
    """A clip whose every sample is ``fill``: a test reads which clip it was by the sample."""
    return wav_of(fill.to_bytes(2, "little", signed=True) * samples, rate)


def say_aloud(identity: str, text: str = "Dinner is ready.", **made: int) -> CommandFrame:
    return CommandFrame(id=identity, command=PlayAudio(text=text, audio=clip(**made)))


def pcm_of_fill(fill: int, samples: int = 100) -> bytes:
    return fill.to_bytes(2, "little", signed=True) * samples


async def settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


async def acked(rig: ClientRig, identity: str) -> Any:
    """What the device answered a command with."""
    async with asyncio.timeout(2):
        while True:
            for ack in rig.connection.frames("ack"):
                if ack.id == identity:
                    return ack.result
            await asyncio.sleep(0)


class SlowOutput(FakeVoiceOutput):
    """A speaker of clips that takes its time, and says if it was stopped in the middle."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cut = False

    async def play(self, audio: AudioPayload, *, alert: bool = False) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cut = True
            raise
        self.played.append(audio)
        self.alerts.append(alert)


class BrokenOutput(FakeVoiceOutput):
    """A speaker of clips that is unplugged: it fails the first time, and plays after that."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def play(self, audio: AudioPayload, *, alert: bool = False) -> None:
        if not self.failed:
            self.failed = True
            raise OSError("the speaker is unplugged")
        await super().play(audio, alert=alert)


class TestWhatADeviceSaysItTakes:
    async def test_a_device_with_a_speaker_says_it_plays_what_a_command_carries(self) -> None:
        for speaker in ({"voice_sink": FakeVoiceSink()}, {"voice_output": FakeVoiceOutput()}):
            rig = ClientRig("speaker", **speaker)
            rig.connection.say(rig.ready(features=PLAYING))

            await rig.client.connect()

            [hello] = rig.connection.frames("hello")
            assert Feature.PLAY_AUDIO in hello.features
            await rig.client.close()

    async def test_a_device_with_no_speaker_does_not(self) -> None:
        rig = ClientRig("screen", voice_sink=FakeVoiceSink())
        rig.connection.say(rig.ready())

        await rig.client.connect()

        [hello] = rig.connection.frames("hello")
        assert hello.features == frozenset()
        await rig.client.close()

    async def test_a_speaker_that_has_nothing_to_play_with_does_not_either(self) -> None:
        rig = ClientRig("speaker")
        rig.connection.say(rig.ready())

        await rig.client.connect()

        [hello] = rig.connection.frames("hello")
        assert hello.features == frozenset()
        await rig.client.close()

    async def test_a_clip_sent_to_a_device_that_did_not_say_it_plays_them_is_the_edges_mistake(
        self,
    ) -> None:
        rig = ClientRig("screen")
        running = await rig.running()

        rig.connection.say(say_aloud("c1"))
        result = await acked(rig, "c1")

        assert result.status is CommandStatus.UNSUPPORTED
        assert result.detail == "this device does not play clips"
        assert rig.hands.commands == []  # the hands have no clips: they are not asked
        assert rig.shown.of("violation") == [
            "the edge sent a clip to a device that did not say it plays them"
        ]
        rig.connection.drop()
        await running

    async def test_nor_is_a_clip_for_a_speaker_that_was_given_nothing_to_play_with(self) -> None:
        rig = ClientRig("speaker")  # it has audio_out, and no sound port: it declared no feature
        running = await rig.running()

        rig.connection.say(say_aloud("c1"))
        result = await acked(rig, "c1")

        assert result.status is CommandStatus.UNSUPPORTED
        assert rig.hands.commands == []
        assert len(rig.shown.of("violation")) == 1
        rig.connection.drop()
        await running


class TestSayingIt:
    async def test_a_clip_is_taken_at_once_and_said_as_a_track(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)

        rig.connection.say(say_aloud("c1", fill=7))
        result = await acked(rig, "c1")
        await rig.client.wait_until_played()

        assert result.status is CommandStatus.OK
        [track] = sink.tracks
        assert bytes(track.pcm) == pcm_of_fill(7)
        assert (track.sample_rate, track.finished, track.aborted) == (22_050, True, False)
        rig.connection.drop()
        await running

    async def test_it_is_said_whole_by_a_speaker_that_only_plays_clips(self) -> None:
        output = FakeVoiceOutput()
        rig = ClientRig("speaker", voice_output=output)
        running = await rig.running(features=PLAYING)

        rig.connection.say(say_aloud("c1", fill=7))
        await acked(rig, "c1")
        await rig.client.wait_until_played()

        assert [clip_.data for clip_ in output.played] == [clip(fill=7).data]
        rig.connection.drop()
        await running

    async def test_a_clip_that_a_command_carried_is_not_an_alert_so_a_mute_silences_it(
        self,
    ) -> None:
        sink, output = FakeVoiceSink(), FakeVoiceOutput()
        for speaker in ({"voice_sink": sink}, {"voice_output": output}):
            rig = ClientRig("speaker", **speaker)
            running = await rig.running(features=PLAYING)

            rig.connection.say(say_aloud("c1", fill=7))
            await acked(rig, "c1")
            await rig.client.wait_until_played()

            rig.connection.drop()
            await running

        assert [track.alert for track in sink.tracks] == [False]
        assert output.alerts == [False]

    async def test_what_is_said_is_written_down_as_an_announcement(self) -> None:
        rig = ClientRig("speaker", voice_sink=FakeVoiceSink())
        running = await rig.running(features=PLAYING)

        rig.connection.say(say_aloud("c1", text="Dinner is ready."))
        await acked(rig, "c1")
        await rig.client.wait_until_played()

        assert rig.shown.of("event") == ["announcement: Dinner is ready."]
        rig.connection.drop()
        await running

    async def test_the_device_answers_before_the_sound_is_played_not_after(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)  # a speaker that takes its time
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)

        rig.connection.say(say_aloud("c1"))
        result = await acked(rig, "c1")

        assert result.status is CommandStatus.OK
        assert rig.client.playing  # and it is saying it still, or about to
        gate.set()
        await rig.client.wait_until_played()
        assert not rig.client.playing
        rig.connection.drop()
        await running

    async def test_what_comes_in_is_said_in_the_order_it_came(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)

        for number, fill in enumerate((1, 2, 3)):
            rig.connection.say(say_aloud(f"c{number}", fill=fill))
        await rig.client.wait_until_played()
        await settle()
        await rig.client.wait_until_played()

        assert [bytes(track.pcm) for track in sink.tracks] == [pcm_of_fill(n) for n in (1, 2, 3)]
        rig.connection.drop()
        await running

    async def test_a_clip_that_is_not_one_is_refused_and_nothing_is_queued(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        not_a_clip = AudioPayload(sample_rate=22_050, data=b"this is not a WAV file")

        rig.connection.say(CommandFrame(id="c1", command=PlayAudio(text="Hi.", audio=not_a_clip)))
        result = await acked(rig, "c1")
        await settle()

        assert result.status is CommandStatus.FAILED
        assert result.detail is not None
        assert "WAV" in result.detail
        assert sink.tracks == []
        assert not rig.client.playing
        rig.connection.drop()
        await running

    async def test_there_is_a_limit_to_what_is_held_and_it_is_of_size_not_of_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "MAX_QUEUED_AUDIO_BYTES", 500)
        rig = ClientRig("speaker", voice_sink=FakeVoiceSink())
        running = await rig.running(features=PLAYING)
        await rig.client.say("hello")  # an answer is being given: nothing is said meanwhile

        for number in range(3):  # 200 bytes of samples each
            rig.connection.say(say_aloud(f"c{number}"))
        results = [await acked(rig, f"c{number}") for number in range(3)]

        assert [result.status for result in results] == [
            CommandStatus.OK,
            CommandStatus.OK,
            CommandStatus.FAILED,
        ]
        assert results[2].detail == "there is too much waiting to be said"
        rig.connection.drop()
        await running


class TestWhatIsHeld:
    async def test_what_fills_what_is_held_exactly_is_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "MAX_QUEUED_AUDIO_BYTES", 400)
        rig = ClientRig("speaker", voice_sink=FakeVoiceSink())
        running = await rig.running(features=PLAYING)
        await rig.client.say("hello")  # an answer is being given: nothing is said meanwhile

        for number in range(3):  # 200 bytes of samples each
            rig.connection.say(say_aloud(f"c{number}"))
        results = [await acked(rig, f"c{number}") for number in range(3)]

        assert [result.status for result in results] == [
            CommandStatus.OK,
            CommandStatus.OK,  # 400 of 400
            CommandStatus.FAILED,
        ]
        rig.connection.drop()
        await running

    async def test_what_has_been_said_is_no_longer_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "MAX_QUEUED_AUDIO_BYTES", 300)
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)

        for number in range(4):  # 200 bytes each: one at a time fits, and never two
            rig.connection.say(say_aloud(f"c{number}"))
            result = await acked(rig, f"c{number}")
            await rig.client.wait_until_played()
            assert result.status is CommandStatus.OK, number

        assert len(sink.tracks) == 4
        rig.connection.drop()
        await running

    async def test_closing_the_client_stops_what_is_said_and_forgets_what_waits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        rig.connection.say(say_aloud("c1", fill=1))
        rig.connection.say(say_aloud("c2", fill=2))
        await asyncio.wait_for(sink.writing.wait(), 2)  # the first is being said, the second waits

        async def not_yet(*_: object) -> None:
            """The reading of the connection does not notice that it was closed until later."""

        monkeypatch.setattr(rig.connection, "close", not_yet)
        await rig.client.close()

        assert not rig.client.playing  # nothing is being said, and nothing is waiting
        gate.set()
        rig.connection.drop()
        await running
        assert len(sink.tracks) == 1  # the second was never begun


class TestWhenItIsSaid:
    async def test_not_while_an_answer_is_being_given_on_the_device_but_when_it_has_been(
        self,
    ) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=STREAMING)
        identity = await rig.client.say("tell me something")

        rig.connection.say(say_aloud("c1", fill=5))
        result = await acked(rig, "c1")
        await settle()

        assert result.status is CommandStatus.OK  # taken, and not said
        assert sink.tracks == []
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)
        await rig.client.wait_until_played()
        assert [bytes(track.pcm) for track in sink.tracks] == [pcm_of_fill(5)]
        rig.connection.drop()
        await running

    async def test_not_while_the_sound_of_an_answer_is_still_playing(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=STREAMING)
        identity = await rig.client.say("tell me something")
        rig.connection.say(PartFrame(utterance_id=identity, seq=0, part=Speech(text="Well.")))
        rig.connection.say(VoiceStart(utterance_id=identity, sample_rate=22_050))
        rig.connection.say(
            VoiceChunk(utterance_id=identity, seq=0, part_seq=0, data=pcm_of_fill(9))
        )
        rig.connection.say(VoiceEnd(utterance_id=identity))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)
        await asyncio.wait_for(
            sink.writing.wait(), 2
        )  # the answer is on its way out of the speaker

        rig.connection.say(say_aloud("c1", fill=5))
        await acked(rig, "c1")
        await settle()

        assert len(sink.tracks) == 1  # the answer's, and nothing of the clip yet
        gate.set()
        await rig.client.wait_until_played()
        assert [bytes(track.pcm) for track in sink.tracks] == [pcm_of_fill(9), pcm_of_fill(5)]
        rig.connection.drop()
        await running

    async def test_the_feedback_of_a_skill_is_said_before_what_the_same_skill_sent(self) -> None:
        # What a person is told ("Done.") is the answer to what they said, and what the skill had
        # said on this device is queued behind it, whichever got to the device first.
        sink = FakeSpeaker()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=STREAMING)
        identity = await rig.client.say("announce that dinner is ready")
        rig.connection.say(say_aloud("c1", fill=5, text="Dinner is ready."))
        await acked(rig, "c1")

        rig.connection.say(PartFrame(utterance_id=identity, seq=0, part=Speech(text="Done.")))
        rig.connection.say(VoiceStart(utterance_id=identity, sample_rate=22_050))
        rig.connection.say(
            VoiceChunk(utterance_id=identity, seq=0, part_seq=0, data=pcm_of_fill(9))
        )
        rig.connection.say(VoiceEnd(utterance_id=identity))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)
        await rig.client.wait_until_played()

        assert [bytes(track.pcm) for track in sink.tracks] == [pcm_of_fill(9), pcm_of_fill(5)]
        rig.connection.drop()
        await running

    async def test_a_device_nobody_is_talking_to_says_it_when_it_comes(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)

        rig.connection.say(say_aloud("c1", fill=4))
        await acked(rig, "c1")
        await rig.client.wait_until_played()

        assert [bytes(track.pcm) for track in sink.tracks] == [pcm_of_fill(4)]
        rig.connection.drop()
        await running


class TestAPersonWhoSpeaks:
    async def test_cuts_what_is_being_said_and_what_waits_is_said_after_what_they_say(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=STREAMING)
        rig.connection.say(say_aloud("c1", fill=1))
        rig.connection.say(say_aloud("c2", fill=2))
        await asyncio.wait_for(sink.writing.wait(), 2)  # the first is being said

        identity = await rig.client.say("stop")  # a person speaks over it

        assert "abort" in sink.calls  # it stopped on the spot
        [first] = sink.tracks
        assert first.aborted
        assert bytes(first.pcm) == b""  # and never got to the end
        gate.set()
        await settle()
        assert len(sink.tracks) == 1  # the second waits for what the person has to say...
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)
        await rig.client.wait_until_played()
        assert bytes(sink.tracks[-1].pcm) == pcm_of_fill(2)  # ...and is said when they are done
        rig.connection.drop()
        await running

    async def test_cuts_a_clip_that_a_speaker_of_clips_is_playing(self) -> None:
        output = SlowOutput()
        rig = ClientRig("speaker", voice_output=output)
        running = await rig.running(features=PLAYING)
        rig.connection.say(say_aloud("c1"))
        await asyncio.wait_for(output.started.wait(), 2)

        await rig.client.say("stop")

        assert output.cut
        assert output.played == []
        rig.connection.drop()
        await running

    async def test_an_answer_that_is_stopped_lets_what_waited_for_it_be_said(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        identity = await rig.client.say("tell me a story")
        rig.connection.say(say_aloud("c1", fill=3))
        await acked(rig, "c1")
        await settle()
        assert sink.tracks == []  # the answer is being given: it waits

        assert await rig.client.cancel()
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.CANCELLED))
        await rig.client.wait_for_turn(identity)
        await rig.client.wait_until_played()

        assert [bytes(track.pcm) for track in sink.tracks] == [pcm_of_fill(3)]
        rig.connection.drop()
        await running


class TestWhatItDoesNotHoldUp:
    async def test_a_command_to_the_hands_is_run_while_a_clip_is_being_said(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        rig.connection.say(say_aloud("c1"))
        await asyncio.wait_for(sink.writing.wait(), 2)

        rig.connection.say(CommandFrame(id="c2", command=SetVolume(level=30)))
        result = await acked(rig, "c2")

        assert result.status is CommandStatus.OK
        assert rig.hands.commands == [SetVolume(level=30)]
        assert rig.client.playing  # the clip is still being said
        gate.set()
        await rig.client.wait_until_played()
        rig.connection.drop()
        await running

    async def test_what_the_edge_sends_after_it_is_read_while_a_clip_is_being_said(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        rig.connection.say(say_aloud("c1"))
        await asyncio.wait_for(sink.writing.wait(), 2)

        rig.connection.say(Pong())
        rig.connection.say(CommandFrame(id="c2", command=SetVolume(level=10)))
        await acked(rig, "c2")

        gate.set()
        await rig.client.wait_until_played()
        rig.connection.drop()
        await running


class TestWhenSomethingGoesWrong:
    async def test_a_speaker_that_fails_says_so_and_the_next_clip_is_tried(self) -> None:
        output = BrokenOutput()
        rig = ClientRig("speaker", voice_output=output)
        running = await rig.running(features=PLAYING)

        rig.connection.say(say_aloud("c1", fill=1))
        rig.connection.say(say_aloud("c2", fill=2))
        await acked(rig, "c2")
        await rig.client.wait_until_played()

        assert [clip_.data for clip_ in output.played] == [clip(fill=2).data]
        assert any("could not say an announcement" in what for what in rig.shown.of("notice"))
        rig.connection.drop()
        await running

    async def test_what_was_waiting_is_given_up_when_the_connection_is_lost(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        await rig.client.say("hello")  # so that what comes waits
        rig.connection.say(say_aloud("c1"))
        await acked(rig, "c1")
        assert rig.client.playing

        rig.connection.drop()
        await running

        assert not rig.client.playing
        assert sink.tracks == []

    async def test_what_was_waiting_is_given_up_when_the_client_is_closed(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        await rig.client.say("hello")
        rig.connection.say(say_aloud("c1"))
        await acked(rig, "c1")

        await rig.client.close()
        await running

        assert not rig.client.playing
        assert sink.tracks == []

    async def test_a_command_the_hands_were_meant_for_still_goes_to_them(self) -> None:
        hands = RecordingCommands()
        rig = ClientRig("speaker", voice_sink=FakeVoiceSink(), hands=hands)
        running = await rig.running(features=PLAYING)

        rig.connection.say(CommandFrame(id="c1", command=SetVolume(level=20)))
        result = await acked(rig, "c1")

        assert result.status is CommandStatus.OK
        assert hands.commands == [SetVolume(level=20)]
        rig.connection.drop()
        await running
