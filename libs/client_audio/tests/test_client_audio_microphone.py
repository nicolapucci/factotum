"""A microphone that is opened when it is needed: what it hands over arrives in a queue that the
loop reads, from the thread of the hardware, and is closed with it."""

import asyncio
import threading
from typing import Any

import numpy as np
import pytest

from factotum.client_audio.backend import AudioUnavailableError
from factotum.client_audio.microphone import Microphone, drain
from factotum.client_audio.testing import FakeBackend


def block(level: int = 1, size: int = 4) -> Any:
    return np.full(size, level, dtype=np.int16)


class TestOpeningIt:
    async def test_it_is_opened_at_the_rate_and_the_block_it_was_asked_for_and_closed_after(
        self,
    ) -> None:
        backend = FakeBackend()
        microphone = Microphone(backend, device="USB", sample_rate=8000, block_size=256)

        async with microphone.opened():
            [stream] = backend.inputs
            assert (stream.device, stream.sample_rate, stream.block_size) == ("USB", 8000, 256)
            assert not stream.closed

        assert stream.closed

    async def test_it_is_closed_when_what_was_done_with_it_failed(self) -> None:
        backend = FakeBackend()
        microphone = Microphone(backend)

        with pytest.raises(RuntimeError):
            async with microphone.opened():
                raise RuntimeError("what was done with it failed")

        assert backend.inputs[0].closed

    async def test_what_it_hears_arrives_in_the_queue_in_order(self) -> None:
        backend = FakeBackend()

        async with Microphone(backend).opened() as blocks:
            for level in (1, 2, 3):
                backend.hearing.hear(block(level))
            heard = [await asyncio.wait_for(blocks.get(), 1) for _ in range(3)]

        assert [each[0] for each in heard] == [1, 2, 3]

    async def test_what_the_thread_of_the_hardware_hands_over_reaches_the_loop(self) -> None:
        backend = FakeBackend()

        async with Microphone(backend).opened() as blocks:
            stream = backend.hearing
            thread = threading.Thread(target=stream.hear, args=(block(7),))
            thread.start()
            heard = await asyncio.wait_for(blocks.get(), 1)
            thread.join()

        assert heard[0] == 7

    async def test_each_time_it_is_opened_is_a_microphone_of_its_own(self) -> None:
        backend = FakeBackend()
        microphone = Microphone(backend)

        async with microphone.opened():
            pass
        async with microphone.opened() as blocks:
            backend.hearing.hear(block(5))
            assert (await asyncio.wait_for(blocks.get(), 1))[0] == 5

        assert len(backend.inputs) == 2

    async def test_a_microphone_that_cannot_be_opened_is_the_error_of_the_backend(self) -> None:
        backend = FakeBackend()
        backend.refuses["open_input"] = "in use by another program"

        with pytest.raises(AudioUnavailableError, match="in use"):
            async with Microphone(backend).opened():
                pass  # pragma: no cover - it is not reached

    def test_the_rate_of_whisper_and_the_block_of_porcupine_are_what_it_is_when_not_said(
        self,
    ) -> None:
        microphone = Microphone(FakeBackend())

        assert (microphone.sample_rate, microphone.block_size) == (16_000, 512)

    def test_a_block_that_arrives_after_the_loop_is_gone_is_let_go_of(self) -> None:
        backend = FakeBackend()

        async def open_and_leave() -> Any:
            async with Microphone(backend).opened():
                return backend.inputs[0]._on_block

        on_block = asyncio.run(open_and_leave())

        on_block(block())  # the hardware has not heard that the loop is closed: it does not fail


class TestForgetting:
    def test_what_was_heard_and_not_taken_is_forgotten_and_counted(self) -> None:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        for level in range(5):
            queue.put_nowait(block(level))

        assert drain(queue) == 5
        assert queue.empty()

    def test_nothing_to_forget_is_nothing(self) -> None:
        assert drain(asyncio.Queue()) == 0
