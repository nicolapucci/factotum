"""What a person says as a client hands it over: chunks of samples, at the pace of a microphone
or as fast as they are taken."""

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.client_core.audio import DEFAULT_CHUNK_MS, AudioStream
from factotum.contracts.audio import MAX_CHUNK_BYTES, MAX_SAMPLE_RATE, MIN_SAMPLE_RATE

RATE = 16000


async def collected(stream: AudioStream) -> list[bytes]:
    return [chunk async for chunk in stream.chunks]


class TestStreamingWhatWasRecorded:
    async def test_samples_are_handed_over_in_chunks_of_the_length_asked_for(self) -> None:
        pcm = bytes(range(250)) * 26  # 6500 bytes: two chunks of 100 ms and a bit

        stream = AudioStream.of(pcm, RATE)
        chunks = await collected(stream)

        assert stream.sample_rate == RATE
        assert [len(chunk) for chunk in chunks] == [3200, 3200, 100]
        assert b"".join(chunks) == pcm

    async def test_a_chunk_is_as_long_as_the_milliseconds_say(self) -> None:
        chunks = await collected(AudioStream.of(bytes(RATE * 2), RATE, chunk_ms=50))

        assert {len(chunk) for chunk in chunks} == {1600}
        assert len(chunks) == 20  # a second of it

    async def test_a_hundred_milliseconds_is_what_a_device_usually_sends(self) -> None:
        assert DEFAULT_CHUNK_MS == 100

    async def test_a_chunk_is_never_more_than_a_chunk_of_the_protocol_may_carry(self) -> None:
        chunks = await collected(AudioStream.of(bytes(400_000), 48000, chunk_ms=5000))

        assert {len(chunk) for chunk in chunks[:-1]} == {MAX_CHUNK_BYTES}
        assert len(b"".join(chunks)) == 400_000

    async def test_no_samples_is_no_chunk(self) -> None:
        assert await collected(AudioStream.of(b"", RATE)) == []

    @given(
        samples=st.integers(0, 3000),
        rate=st.integers(MIN_SAMPLE_RATE, MAX_SAMPLE_RATE),
        chunk_ms=st.integers(1, 5000),
    )
    def test_whatever_the_rate_and_the_pace_the_samples_arrive_whole_and_in_order(
        self, samples: int, rate: int, chunk_ms: int
    ) -> None:
        pcm = bytes(i % 256 for i in range(2 * samples))

        chunks = asyncio.run(collected(AudioStream.of(pcm, rate, chunk_ms=chunk_ms)))

        assert b"".join(chunks) == pcm
        assert all(len(chunk) % 2 == 0 for chunk in chunks)  # a sample is never cut in half
        assert all(0 < len(chunk) <= MAX_CHUNK_BYTES for chunk in chunks)

    async def test_at_the_pace_of_a_microphone_each_chunk_takes_the_time_it_takes_to_say_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", sleep)

        await collected(AudioStream.of(bytes(3200 * 2 + 1600), RATE, realtime=True))

        assert slept == pytest.approx([0.1, 0.1, 0.05])

    async def test_otherwise_it_does_not_wait_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", sleep)

        await collected(AudioStream.of(bytes(3200 * 5), RATE))

        assert slept == []
