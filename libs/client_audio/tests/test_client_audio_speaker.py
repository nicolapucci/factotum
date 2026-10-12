"""A speaker that plays a clip whole or a track as it arrives, at a volume of its own, with a sound
card that a test plays: what it was given, in what order, and what it does when it is told to stop
while it waits for the device."""

import asyncio
import threading
from typing import Any

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.client_audio.backend import AudioUnavailableError
from factotum.client_audio.speaker import CLIP_CHUNK_MS, FULL_VOLUME, Speaker, gain_of
from factotum.client_audio.testing import FakeBackend
from factotum.client_core.audio import MarkedSink, VoiceOutput, VoiceSink
from factotum.contracts.audio import wav_of

RATE = 22_050


def samples(*values: int) -> bytes:
    return np.array(values, dtype="<i2").tobytes()


def heard_by(backend: FakeBackend) -> list[int]:
    return np.frombuffer(backend.outputs[-1].sound, dtype="<i2").tolist()


class TestWhatAVolumeDoes:
    @pytest.mark.parametrize(
        ("level", "gain"), [(100, 1.0), (50, 0.25), (10, 0.01), (0, 0.0), (150, 1.0), (-5, 0.0)]
    )
    def test_the_gain_is_the_square_of_the_share_of_the_full_volume(
        self, level: int, gain: float
    ) -> None:
        assert gain_of(level) == pytest.approx(gain)

    def test_the_full_volume_changes_nothing_of_what_is_given(self) -> None:
        speaker = Speaker(FakeBackend())
        pcm = samples(1, -2, 32767, -32768)

        assert speaker.scaled(pcm) is pcm

    def test_half_the_volume_is_a_quarter_of_the_amplitude(self) -> None:
        speaker = Speaker(FakeBackend(), volume=50)

        assert np.frombuffer(speaker.scaled(samples(1000, -1000, 20000)), "<i2").tolist() == [
            250,
            -250,
            5000,
        ]

    def test_what_is_scaled_never_overflows(self) -> None:
        speaker = Speaker(FakeBackend(), volume=100)
        speaker.set_volume(100)

        assert speaker.scaled(samples(32767, -32768)) == samples(32767, -32768)

    def test_the_volume_is_kept_between_nothing_and_the_full(self) -> None:
        speaker = Speaker(FakeBackend(), volume=250)
        assert speaker.volume == FULL_VOLUME

        speaker.set_volume(-3)
        assert speaker.volume == 0

        speaker.set_volume(30)
        assert speaker.volume == 30

    def test_a_speaker_with_no_volume_is_silent(self) -> None:
        speaker = Speaker(FakeBackend(), volume=0)

        assert speaker.scaled(samples(1000, -1000)) == samples(0, 0)

    def test_the_volume_goes_up_and_down_by_some_points_and_the_new_level_is_given(self) -> None:
        speaker = Speaker(FakeBackend(), volume=50)

        assert speaker.adjust_volume(10) == 60
        assert speaker.volume == 60
        assert speaker.adjust_volume(-25) == 35
        assert speaker.volume == 35

    def test_it_is_never_taken_past_either_end(self) -> None:
        speaker = Speaker(FakeBackend(), volume=95)

        assert speaker.adjust_volume(10) == FULL_VOLUME
        assert speaker.adjust_volume(100) == FULL_VOLUME
        speaker.set_volume(5)
        assert speaker.adjust_volume(-10) == 0
        assert speaker.adjust_volume(-100) == 0

    @given(st.integers(0, 100), st.integers(-100, 100))
    def test_the_level_that_results_is_the_sum_kept_between_nothing_and_the_full(
        self, level: int, delta: int
    ) -> None:
        speaker = Speaker(FakeBackend(), volume=level)

        assert speaker.adjust_volume(delta) == max(0, min(level + delta, FULL_VOLUME))

    def test_what_is_played_after_it_is_at_the_new_level(self) -> None:
        speaker = Speaker(FakeBackend(), volume=100)

        speaker.adjust_volume(-50)

        assert np.frombuffer(speaker.scaled(samples(1000, -1000)), "<i2").tolist() == [250, -250]


class TestPlayingATrack:
    async def test_what_is_written_is_played_in_order_and_the_track_is_finished(self) -> None:
        backend = FakeBackend()
        speaker = Speaker(backend)

        await speaker.begin(RATE)
        await speaker.write(samples(1, 2))
        await speaker.write(samples(3))
        await speaker.finish()

        [output] = backend.outputs
        assert output.sample_rate == RATE
        assert heard_by(backend) == [1, 2, 3]
        assert output.calls == ["write", "write", "stop", "close"]  # stop waits for what is played

    async def test_it_is_a_speaker_of_the_device_that_was_chosen(self) -> None:
        backend = FakeBackend()

        await Speaker(backend, device="USB").begin(RATE)

        assert backend.outputs[0].device == "USB"

    async def test_the_volume_is_what_the_device_is_given(self) -> None:
        backend = FakeBackend()
        speaker = Speaker(backend, volume=50)

        await speaker.begin(RATE)
        await speaker.write(samples(1000))
        speaker.set_volume(100)  # a person turned it up in the middle of what it was saying
        await speaker.write(samples(1000))
        await speaker.finish()

        assert heard_by(backend) == [250, 1000]

    async def test_a_track_that_follows_another_is_a_stream_of_its_own_at_its_own_rate(
        self,
    ) -> None:
        backend = FakeBackend()
        speaker = Speaker(backend)

        for rate in (RATE, 16_000):
            await speaker.begin(rate)
            await speaker.write(samples(1))
            await speaker.finish()

        assert [output.sample_rate for output in backend.outputs] == [RATE, 16_000]

    async def test_a_track_that_began_while_another_was_going_on_lets_the_first_go(self) -> None:
        backend = FakeBackend()
        speaker = Speaker(backend)
        await speaker.begin(RATE)
        await speaker.write(samples(1))

        await speaker.begin(RATE)

        assert backend.outputs[0].calls == ["write", "abort", "close"]
        assert len(backend.outputs) == 2

    async def test_samples_for_a_track_that_was_not_begun_are_an_error(self) -> None:
        speaker = Speaker(FakeBackend())

        with pytest.raises(AudioUnavailableError, match="not begun"):
            await speaker.write(samples(1))

    async def test_finishing_or_stopping_what_is_not_playing_is_nothing(self) -> None:
        backend = FakeBackend()
        speaker = Speaker(backend)

        await speaker.finish()
        await speaker.abort()

        assert backend.outputs == []

    async def test_a_speaker_that_cannot_be_opened_is_the_error_of_the_backend(self) -> None:
        backend = FakeBackend()
        backend.refuses["open_output"] = "no such device"

        with pytest.raises(AudioUnavailableError, match="no such device"):
            await Speaker(backend).begin(RATE)

    async def test_it_is_the_three_things_a_client_can_be_given_to_speak_with(self) -> None:
        speaker = Speaker(FakeBackend())

        assert isinstance(speaker, MarkedSink)  # a sink, and told which sentence
        sink: VoiceSink = speaker
        output: VoiceOutput = speaker  # a clip, for an edge that does not stream
        assert sink is speaker
        assert output is speaker


class TestStoppingIt:
    async def test_a_write_that_waits_for_the_device_goes_when_it_is_told_to_stop(self) -> None:
        backend = FakeBackend()
        backend.gate = threading.Event()  # a device that takes its time
        speaker = Speaker(backend)
        await speaker.begin(RATE)
        writing = asyncio.ensure_future(speaker.write(samples(1, 2, 3)))
        await asyncio.wait_for(asyncio.to_thread(backend.outputs[0].waiting.wait, 2), 3)

        await asyncio.wait_for(speaker.abort(), 2)

        with pytest.raises(AudioUnavailableError, match="told to stop"):
            await asyncio.wait_for(writing, 2)
        assert backend.outputs[0].calls == ["write", "abort", "close"]
        assert heard_by(backend) == []  # what it was waiting to take was dropped

    async def test_stopping_is_not_waiting_for_what_was_written_to_be_played(self) -> None:
        backend = FakeBackend()
        speaker = Speaker(backend)
        await speaker.begin(RATE)
        await speaker.write(samples(1))

        await speaker.abort()

        assert "stop" not in backend.outputs[0].calls  # stop would wait for it

    async def test_a_track_that_is_stopped_is_no_longer_one_that_can_be_written_to(self) -> None:
        speaker = Speaker(FakeBackend())
        await speaker.begin(RATE)
        await speaker.abort()

        with pytest.raises(AudioUnavailableError):
            await speaker.write(samples(1))

    async def test_the_device_is_closed_whatever_it_says_when_told_to_stop(self) -> None:
        backend = FakeBackend()
        speaker = Speaker(backend)
        await speaker.begin(RATE)
        output = backend.outputs[0]

        def refuse() -> None:
            output.calls.append("abort")
            raise AudioUnavailableError("the device is gone")

        output.abort = refuse  # type: ignore[method-assign]

        with pytest.raises(AudioUnavailableError):
            await speaker.abort()

        assert output.calls == ["abort", "close"]


class TestTheSentenceThatIsBeingSaid:
    async def test_it_is_what_the_last_mark_said_until_the_track_is_over(self) -> None:
        speaker = Speaker(FakeBackend())
        await speaker.begin(RATE)
        assert speaker.sentence is None

        await speaker.mark(0)
        await speaker.write(samples(1))
        await speaker.mark(1)
        assert speaker.sentence == 1

        await speaker.finish()
        assert speaker.sentence is None

    async def test_it_is_forgotten_when_the_track_is_stopped(self) -> None:
        speaker = Speaker(FakeBackend())
        await speaker.begin(RATE)
        await speaker.mark(3)

        await speaker.abort()

        assert speaker.sentence is None


class TestPlayingAClip:
    async def test_a_clip_is_played_whole_a_tenth_of_a_second_at_a_time(self) -> None:
        backend = FakeBackend()
        speaker = Speaker(backend)
        size = RATE * CLIP_CHUNK_MS // 1000
        pcm = samples(*range(1, size * 2 + 11))  # two tenths of a second and a few samples

        await speaker.play(wav_of(pcm, RATE))

        [output] = backend.outputs
        assert [len(write) // 2 for write in output.writes] == [size, size, 10]
        assert output.sound == pcm
        assert output.calls[-2:] == ["stop", "close"]

    async def test_what_is_written_is_whole_samples_at_a_rate_that_has_no_whole_tenth(self) -> None:
        backend = FakeBackend()
        pcm = samples(*range(1, 2500))  # a tenth of a second at 11025 Hz is 1102.5 samples

        await Speaker(backend, volume=50).play(wav_of(pcm, 11_025))

        [output] = backend.outputs
        assert [len(write) for write in output.writes] == [2204, 2204, 590]
        assert len(output.sound) == len(pcm)

    async def test_a_clip_that_is_cut_short_stops_the_speaker_and_says_so(self) -> None:
        backend = FakeBackend()
        backend.gate = threading.Event()
        speaker = Speaker(backend)
        playing = asyncio.ensure_future(speaker.play(wav_of(samples(1, 2, 3), RATE)))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(asyncio.to_thread(backend.outputs[0].waiting.wait, 2), 3)

        playing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await playing

        assert backend.outputs[0].calls[-2:] == ["abort", "close"]

    async def test_a_clip_that_is_not_16_bit_mono_is_not_played(self) -> None:
        backend = FakeBackend()
        not_a_clip: Any = type("Clip", (), {"data": b"not a wav file"})()

        with pytest.raises(ValueError, match="not a WAV file"):
            await Speaker(backend).play(not_a_clip)

        assert backend.outputs == []

    async def test_a_clip_is_played_at_the_volume_of_the_speaker(self) -> None:
        backend = FakeBackend()

        await Speaker(backend, volume=50).play(wav_of(samples(1000, 2000), RATE))

        assert heard_by(backend) == [250, 500]
