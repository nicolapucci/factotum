"""The sound of an answer given to a speaker as it arrives: in order, without holding up whatever
gives it, and let go of at once when the person speaks over it (ADR 0023)."""

import asyncio

import pytest

from factotum.client_core.playback import VoicePlayer
from factotum.client_core.testing import FakeMarkedSink, FakeVoiceSink
from factotum.contracts.audio import pcm_of


class Failures:
    """What a player said about a speaker that failed."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def __call__(self, why: str) -> None:
        self.said.append(why)


def a_player(sink: FakeVoiceSink | None = None) -> tuple[VoicePlayer, FakeVoiceSink, Failures]:
    sink = sink or FakeVoiceSink()
    failures = Failures()
    return VoicePlayer(sink, failures), sink, failures


class TestPlayingATrack:
    async def test_what_is_given_is_played_in_order_and_the_track_is_finished(self) -> None:
        player, sink, _ = a_player()

        player.begin(22050)
        player.write(b"\x01\x00")
        player.write(b"\x02\x00")
        player.end()
        await player.idle()

        assert sink.calls == ["begin", "write", "write", "finish"]
        [track] = sink.tracks
        assert (track.sample_rate, bytes(track.pcm), track.finished) == (
            22050,
            b"\x01\x00\x02\x00",
            True,
        )

    async def test_a_track_is_not_an_alert_unless_it_is_begun_as_one(self) -> None:
        player, sink, _ = a_player()

        player.begin(22050)
        player.write(b"\x01\x00")
        player.end()
        player.begin(22050, alert=True)
        player.write(b"\x02\x00")
        player.end()
        player.begin(22050)
        player.write(b"\x03\x00")
        player.end()
        await player.idle()

        assert [track.alert for track in sink.tracks] == [False, True, False]

    async def test_two_tracks_one_after_the_other_are_two_tracks_of_the_speaker(self) -> None:
        player, sink, _ = a_player()

        player.begin(22050)
        player.write(b"\x01\x00")
        player.end()
        player.begin(16000)
        player.write(b"\x02\x00")
        player.end()
        await player.idle()

        assert [(t.sample_rate, bytes(t.pcm)) for t in sink.tracks] == [
            (22050, b"\x01\x00"),
            (16000, b"\x02\x00"),
        ]

    async def test_nothing_given_is_nothing_to_wait_for(self) -> None:
        player, sink, _ = a_player()

        await asyncio.wait_for(player.idle(), 1)

        assert not player.playing
        assert sink.calls == []

    async def test_it_is_playing_from_when_something_is_given_until_it_has_been_played(
        self,
    ) -> None:
        gate = asyncio.Event()
        player, sink, _ = a_player(FakeVoiceSink(gate=gate))

        player.begin(22050)
        player.write(b"\x01\x00")
        await asyncio.wait_for(sink.writing.wait(), 1)

        assert player.playing
        gate.set()
        player.end()
        await player.idle()
        assert not player.playing

    async def test_giving_sound_does_not_wait_for_the_speaker_to_take_it(self) -> None:
        gate = asyncio.Event()
        player, sink, _ = a_player(FakeVoiceSink(gate=gate))

        player.begin(22050)
        for _ in range(50):
            player.write(b"\x01\x00")  # none of it can be played yet, and none of it blocks

        await asyncio.wait_for(sink.writing.wait(), 1)
        assert sink.calls.count("write") == 1  # the speaker holds the first and the rest wait
        gate.set()
        player.end()
        await player.idle()
        assert sink.calls.count("write") == 50
        assert bytes(sink.tracks[0].pcm) == b"\x01\x00" * 50


class TestASpeakerThatWantsToKnowTheSentences:
    async def test_it_is_told_where_each_sentence_begins_before_the_first_of_its_samples(
        self,
    ) -> None:
        sink = FakeMarkedSink()
        player, _, _ = a_player(sink)

        player.begin(22050)
        player.write(b"\x01\x00", 3)
        player.write(b"\x02\x00", 3)
        player.write(b"\x03\x00", 5)
        player.end()
        await player.idle()

        assert sink.calls == ["begin", "mark", "write", "write", "mark", "write", "finish"]
        [track] = sink.tracks
        assert [(part, bytes(pcm)) for part, pcm in track.sentences] == [
            (3, b"\x01\x00\x02\x00"),
            (5, b"\x03\x00"),
        ]

    async def test_a_new_track_begins_with_a_sentence_of_its_own_even_of_the_same_number(
        self,
    ) -> None:
        sink = FakeMarkedSink()
        player, _, _ = a_player(sink)

        player.begin(22050)
        player.write(b"\x01\x00", 1)
        player.end()
        player.begin(16000)
        player.write(b"\x02\x00", 1)
        player.end()
        await player.idle()

        assert sink.calls.count("mark") == 2

    async def test_what_it_was_given_can_be_read_back_a_sentence_at_a_time(self) -> None:
        sink = FakeMarkedSink()
        player, _, _ = a_player(sink)
        player.begin(22050)
        player.write(b"\x01\x00", 0)
        player.write(b"\x02\x00", 2)
        player.end()
        await player.idle()

        first, second = sink.sentences()

        assert (first.sample_rate, pcm_of(first.data)[0]) == (22050, b"\x01\x00")
        assert pcm_of(second.data)[0] == b"\x02\x00"

    async def test_a_speaker_that_does_not_ask_is_not_told(self) -> None:
        player, sink, _ = a_player()  # a plain speaker

        player.begin(22050)
        player.write(b"\x01\x00", 4)
        player.end()
        await player.idle()

        assert "mark" not in sink.calls


class TestATrackThatHasNotEnded:
    async def test_it_is_playing_though_everything_that_was_given_has_been_taken(self) -> None:
        player, sink, _ = a_player()

        player.begin(22050)
        player.write(b"\x01\x00")
        await asyncio.sleep(0.01)  # the speaker has taken both, and has more to come

        assert sink.calls == ["begin", "write"]
        assert player.playing

    async def test_it_is_cut_when_stopped_and_not_waited_for_when_it_never_ends(self) -> None:
        player, sink, _ = a_player()
        player.begin(22050)
        player.write(b"\x01\x00")
        await asyncio.sleep(0.01)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(player.idle(), 0.05)  # a track that does not end is not over
        await player.stop()

        assert sink.calls == ["begin", "write", "abort"]
        assert not player.playing
        await asyncio.wait_for(player.idle(), 1)


class TestStoppingIt:
    async def test_what_is_playing_is_cut_and_what_was_queued_is_never_played(self) -> None:
        gate = asyncio.Event()
        player, sink, _ = a_player(FakeVoiceSink(gate=gate))
        player.begin(22050)
        player.write(b"\x01\x00")
        player.write(b"\x02\x00")
        player.write(b"\x03\x00")
        player.end()
        await asyncio.wait_for(sink.writing.wait(), 1)

        await player.stop()
        gate.set()
        await asyncio.sleep(0.01)

        assert sink.calls == ["begin", "write", "abort"]
        assert not player.playing
        assert sink.tracks[0].aborted

    async def test_it_can_be_stopped_while_the_speaker_is_taking_its_time(self) -> None:
        player, sink, _ = a_player(FakeVoiceSink(gate=asyncio.Event()))  # a gate that never opens
        player.begin(22050)
        player.write(b"\x01\x00")
        await asyncio.wait_for(sink.writing.wait(), 1)

        await asyncio.wait_for(player.stop(), 1)

        assert not player.playing

    async def test_a_player_that_is_not_playing_does_not_tell_the_speaker_to_stop(self) -> None:
        player, sink, _ = a_player()

        await player.stop()

        assert sink.calls == []

    async def test_a_player_that_has_played_everything_does_not_either(self) -> None:
        player, sink, _ = a_player()
        player.begin(22050)
        player.end()
        await player.idle()

        await player.stop()

        assert sink.calls == ["begin", "finish"]

    async def test_what_comes_after_is_played_as_if_nothing_had_happened(self) -> None:
        gate = asyncio.Event()
        player, sink, _ = a_player(FakeVoiceSink(gate=gate))
        player.begin(22050)
        player.write(b"\x01\x00")
        player.write(b"\x02\x00")  # queued behind the one that the speaker is taking its time with
        player.end()
        await asyncio.wait_for(sink.writing.wait(), 1)
        await player.stop()
        gate.set()

        player.begin(16000)
        player.write(b"\x09\x00")
        player.end()
        await asyncio.wait_for(player.idle(), 1)

        assert [(t.sample_rate, bytes(t.pcm), t.finished) for t in sink.tracks] == [
            (22050, b"", False),  # what was queued when it was stopped is not played later
            (16000, b"\x09\x00", True),
        ]

    async def test_a_speaker_that_cannot_stop_has_stopped(self) -> None:
        class Stubborn(FakeVoiceSink):
            async def abort(self) -> None:
                raise OSError("the speaker is gone")

        player, sink, failures = a_player(Stubborn(gate=asyncio.Event()))
        player.begin(22050)
        player.write(b"\x01\x00")
        await asyncio.wait_for(sink.writing.wait(), 1)

        await player.stop()

        assert not player.playing
        assert failures.said == []  # nothing was lost that the person would have heard


class TestASpeakerThatFails:
    @pytest.mark.parametrize("call", ["begin", "write", "finish"])
    async def test_the_track_it_failed_in_is_lost_and_said_and_the_player_goes_on(
        self, call: str
    ) -> None:
        player, sink, failures = a_player()
        sink.fails[call] = OSError("the speaker was unplugged")

        player.begin(22050)
        player.write(b"\x01\x00")
        player.end()
        await asyncio.wait_for(player.idle(), 1)

        assert failures.said == ["the speaker could not play the sound: the speaker was unplugged"]
        assert not player.playing

    async def test_the_rest_of_a_track_that_failed_is_not_given_to_the_speaker(self) -> None:
        player, sink, failures = a_player()
        sink.fails["write"] = OSError("busy")

        player.begin(22050)
        player.write(b"\x01\x00")
        player.write(b"\x02\x00")
        player.write(b"\x03\x00")
        player.end()
        await asyncio.wait_for(player.idle(), 1)

        assert sink.calls == ["begin", "write"]  # nothing after the one that failed
        assert len(failures.said) == 1

    async def test_the_next_track_is_tried_again(self) -> None:
        player, sink, failures = a_player()
        sink.fails["write"] = OSError("busy")
        player.begin(22050)
        player.write(b"\x01\x00")
        player.end()

        player.begin(22050)
        player.write(b"\x02\x00")
        player.end()
        await asyncio.wait_for(player.idle(), 1)

        assert [(bytes(t.pcm), t.finished) for t in sink.tracks] == [
            (b"", False),
            (b"\x02\x00", True),
        ]
        assert len(failures.said) == 1
