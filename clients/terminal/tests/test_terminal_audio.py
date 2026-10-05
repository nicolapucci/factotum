"""What a person says as the terminal hands it over: chunks of samples, and where speech ends.

The microphone itself needs hardware and is tried by hand. What decides where a stream of samples
ends does not: it is given frames the way a microphone would hand them over."""

import asyncio
from typing import Any

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.contracts.audio import MAX_CHUNK_BYTES, MAX_SAMPLE_RATE, MIN_SAMPLE_RATE
from factotum.terminal.audio import DEFAULT_CHUNK_MS, AudioStream, capture_speech

FRAME = 512
"""The samples in a frame of Porcupine's, at 16 kHz: 32 ms."""

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


def loud(index: int = 0) -> Any:
    """A frame of somebody talking: every sample different from the other frames' and far above
    the level of silence."""
    return np.full(FRAME, 1000 + index, dtype=np.int16)


def quiet(level: int = 0) -> Any:
    return np.full(FRAME, level, dtype=np.int16)


def queue_of(*frames: Any) -> "asyncio.Queue[Any]":
    queue: asyncio.Queue[Any] = asyncio.Queue()
    for frame in frames:
        queue.put_nowait(frame)
    return queue


def speech_of(frames: "asyncio.Queue[Any]", **changes: float) -> Any:
    fields: dict[str, float] = {"silence_seconds": 0.5, "silence_level": 150, "max_seconds": 5.0}
    return capture_speech(frames, RATE, **{**fields, **changes})


def samples_of(chunks: list[bytes]) -> Any:
    return np.frombuffer(b"".join(chunks), dtype="<i2")


class TestWhereSpeechEnds:
    async def test_what_is_said_comes_in_chunks_of_about_a_hundred_milliseconds_until_the_quiet(
        self,
    ) -> None:
        # 15 quiet frames are 0.48 s: of the 0.5 s of quiet that ends it, as many frames as fit.
        frames = [loud(i) for i in range(12)] + [quiet()] * 15 + [loud(99)] * 5
        queue = queue_of(*frames)

        chunks = [chunk async for chunk in speech_of(queue)]

        assert [len(chunk) // 2 // FRAME for chunk in chunks] == [4, 4, 4, 4, 4, 4, 3]
        assert queue.qsize() == 5  # what came after is not taken
        assert samples_of(chunks).tolist() == np.concatenate(frames[:27]).tolist()

    async def test_the_samples_are_16_bit_little_endian_whatever_the_machine(self) -> None:
        queue = queue_of(*[np.full(FRAME, -2, dtype=np.int16)] * 4)

        chunks = [chunk async for chunk in speech_of(queue, max_seconds=0.13)]

        assert chunks == [b"\xfe\xff" * FRAME * 4]

    async def test_it_ends_after_as_long_as_it_may_whether_or_not_the_person_has_stopped(
        self,
    ) -> None:
        queue = queue_of(*[loud(i) for i in range(40)])

        chunks = [chunk async for chunk in speech_of(queue, max_seconds=0.5)]

        # 0.5 s of frames of 32 ms is 15 of them
        assert len(b"".join(chunks)) // 2 // FRAME == 15
        assert queue.qsize() == 25

    async def test_a_pause_shorter_than_the_quiet_that_ends_it_does_not_end_it(self) -> None:
        pause = [quiet()] * 10
        frames = pause + [loud(1)] + pause + [loud(2)] + [quiet()] * 15 + [loud(3)]
        queue = queue_of(*frames)

        chunks = [chunk async for chunk in speech_of(queue)]

        assert len(b"".join(chunks)) // 2 // FRAME == 37  # up to the quiet that did end it
        assert queue.qsize() == 1

    @pytest.mark.parametrize(
        ("level", "taken"),
        [(149, 15), (150, 28)],
        ids=["under the level is quiet: it ends", "at the level is not: it goes on to the most"],
    )
    async def test_quiet_is_a_mean_level_under_the_one_given(self, level: int, taken: int) -> None:
        queue = queue_of(*[quiet(level)] * 40)

        [chunk async for chunk in speech_of(queue, max_seconds=0.9)]

        assert 40 - queue.qsize() == taken

    async def test_a_listener_that_stops_reading_leaves_the_rest_of_the_frames_alone(self) -> None:
        queue = queue_of(*[loud(i) for i in range(20)])
        speech = speech_of(queue)

        first = await anext(speech)
        await speech.aclose()

        assert len(first) // 2 // FRAME == 4
        assert queue.qsize() == 16

    async def test_it_waits_for_frames_that_have_not_come_yet(self) -> None:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        speech = speech_of(queue, max_seconds=0.13)

        taking = asyncio.ensure_future(anext(speech))
        await asyncio.sleep(0.01)
        assert not taking.done()
        for index in range(4):
            queue.put_nowait(loud(index))
        chunk = await asyncio.wait_for(taking, 1)

        assert len(chunk) // 2 // FRAME == 4
