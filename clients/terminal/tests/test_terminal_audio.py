"""Where speech ends, for the terminal's real microphone.

The microphone itself needs hardware and is tried by hand. What decides where a stream of samples
ends does not: it is given frames the way a microphone would hand them over."""

import asyncio
from typing import Any

import numpy as np
import pytest

from factotum.terminal.audio import capture_speech

FRAME = 512
"""The samples in a frame of Porcupine's, at 16 kHz: 32 ms."""

RATE = 16000


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
