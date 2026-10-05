"""Samples and clips: what a stream is made of, and the WAV file the edge puts it in."""

import io
import wave

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from factotum.contracts.audio import (
    MAX_AUDIO_BYTES,
    MAX_CHUNK_BYTES,
    MAX_SAMPLE_RATE,
    MAX_STREAM_BYTES,
    MIN_SAMPLE_RATE,
    WAV_HEADER_BYTES,
    AudioFormat,
    StreamFormat,
    pcm_of,
    wav_of,
)


def wav(
    *, channels: int = 1, width: int = 2, rate: int = 16000, frames: bytes = b"\x00\x00"
) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(rate)
        wav_file.writeframes(frames)
    return buffer.getvalue()


class TestTheLimits:
    def test_a_stream_is_about_a_minute_of_what_the_models_read(self) -> None:
        assert 60 < MAX_STREAM_BYTES / (16000 * 2) < 70

    def test_the_most_a_stream_holds_and_the_header_of_its_clip_are_what_a_clip_may_hold(
        self,
    ) -> None:
        assert MAX_STREAM_BYTES + WAV_HEADER_BYTES == MAX_AUDIO_BYTES

    def test_a_chunk_is_a_couple_of_seconds_at_most(self) -> None:
        assert 1 < MAX_CHUNK_BYTES / (16000 * 2) < 3

    def test_the_header_is_what_the_standard_library_writes(self) -> None:
        assert len(wav_of(bytes(100), 16000).data) == 100 + WAV_HEADER_BYTES

    def test_the_rates_of_a_clip_are_the_rates_of_a_stream(self) -> None:
        assert (MIN_SAMPLE_RATE, MAX_SAMPLE_RATE) == (8000, 48000)


class TestPuttingSamplesInAWavFile:
    def test_the_samples_become_a_clip_that_the_standard_library_reads(self) -> None:
        pcm = bytes(range(256)) * 4  # 512 samples

        clip = wav_of(pcm, 16000)

        assert (clip.format, clip.sample_rate) == (AudioFormat.WAV, 16000)
        with wave.open(io.BytesIO(clip.data), "rb") as wav_file:
            assert wav_file.getnchannels() == 1
            assert wav_file.getsampwidth() == 2
            assert wav_file.getframerate() == 16000
            assert wav_file.getnframes() == 512
            assert wav_file.readframes(512) == pcm

    def test_the_most_a_stream_holds_still_makes_a_clip(self) -> None:
        clip = wav_of(bytes(MAX_STREAM_BYTES), 16000)

        assert len(clip.data) == MAX_AUDIO_BYTES

    def test_more_than_a_stream_may_hold_is_more_than_a_clip_may(self) -> None:
        with pytest.raises(ValidationError):
            wav_of(bytes(MAX_STREAM_BYTES + 2), 16000)

    def test_half_a_sample_at_the_end_is_left_out(self) -> None:
        clip = wav_of(b"\x01\x00\x02", 16000)

        assert pcm_of(clip.data) == (b"\x01\x00", 16000)

    def test_no_samples_is_a_clip_with_a_header_and_nothing_in_it(self) -> None:
        clip = wav_of(b"", 16000)

        assert len(clip.data) == WAV_HEADER_BYTES
        assert pcm_of(clip.data) == (b"", 16000)

    @given(
        samples=st.binary(max_size=4000).map(lambda data: data[: len(data) - len(data) % 2]),
        rate=st.integers(MIN_SAMPLE_RATE, MAX_SAMPLE_RATE),
    )
    def test_what_goes_into_a_wav_file_comes_out_of_it(self, samples: bytes, rate: int) -> None:
        assert pcm_of(wav_of(samples, rate).data) == (samples, rate)


class TestReadingSamplesOutOfAWavFile:
    def test_the_rate_is_the_one_of_the_file(self) -> None:
        assert pcm_of(wav(rate=22050, frames=b"\x01\x02\x03\x04")) == (b"\x01\x02\x03\x04", 22050)

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(wav(channels=2, frames=b"\x00\x00\x00\x00"), id="stereo"),
            pytest.param(wav(width=1, frames=b"\x00"), id="8-bit"),
            pytest.param(wav(width=4, frames=b"\x00\x00\x00\x00"), id="32-bit"),
        ],
    )
    def test_only_16_bit_mono_is_what_a_stream_carries(self, data: bytes) -> None:
        with pytest.raises(ValueError, match="16-bit mono"):
            pcm_of(data)

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"", id="nothing"),
            pytest.param(b"not a wav file at all", id="not a wav"),
            pytest.param(wav()[:20], id="cut short"),
        ],
    )
    def test_what_is_not_a_wav_file_is_a_value_error_and_not_what_the_library_raises(
        self, data: bytes
    ) -> None:
        with pytest.raises(ValueError, match="not a WAV file"):
            pcm_of(data)


class TestTheFormatOfAStream:
    def test_the_samples_of_a_stream_are_16_bit_little_endian_and_nothing_else(self) -> None:
        assert [f.value for f in StreamFormat] == ["pcm_s16le"]
