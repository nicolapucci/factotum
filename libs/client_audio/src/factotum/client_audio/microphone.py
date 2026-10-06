"""A microphone that is opened when it is needed and closed when it is not.

What a microphone hands over arrives on a thread of the hardware's own, a block at a time; here it
is put in a queue that the loop of the client reads, which is all a thread of the hardware may do
(it must not wait for anything). What reads the queue decides what to do with the blocks: look for a
wake word in them, or find where a person's speech ends (``factotum.client_audio.endpoint``).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from factotum.client_audio.backend import AudioBackend, DeviceChoice

DEFAULT_RATE = 16_000
"""What Whisper and the wake word hear at: a microphone is opened at it, and the system brings what
it hears to it."""

DEFAULT_BLOCK = 512
"""The samples in a block: 32 ms at 16 kHz, which is what Porcupine takes at a time."""


class Microphone:
    """A microphone of the system (``device``: its index, a part of its name, or None for the
    default), opened at ``sample_rate`` and handing over blocks of ``block_size`` samples."""

    def __init__(
        self,
        backend: AudioBackend,
        *,
        device: DeviceChoice = None,
        sample_rate: int = DEFAULT_RATE,
        block_size: int = DEFAULT_BLOCK,
    ) -> None:
        self._backend = backend
        self._device = device
        self.sample_rate = sample_rate
        self.block_size = block_size

    @asynccontextmanager
    async def opened(self) -> AsyncIterator["asyncio.Queue[Any]"]:
        """The microphone, hearing: the queue that its blocks arrive in, until the block is left.
        It is closed when it is, and what arrives is not kept after it. Raises
        ``AudioUnavailableError`` when it cannot be opened."""
        loop = asyncio.get_running_loop()
        blocks: asyncio.Queue[Any] = asyncio.Queue()

        def on_block(block: Any) -> None:
            # The loop may be gone, and the hardware has not heard of it yet.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(blocks.put_nowait, block)

        stream = await asyncio.to_thread(
            self._backend.open_input, self._device, self.sample_rate, self.block_size, on_block
        )
        try:
            yield blocks
        finally:
            await asyncio.to_thread(stream.close)


def drain(blocks: "asyncio.Queue[Any]") -> int:
    """Forget what has been heard and not taken: what arrived while the assistant was answering is
    its own voice and a room's, and is nothing to look for a wake word in. How many it was."""
    forgotten = 0
    while not blocks.empty():
        blocks.get_nowait()
        forgotten += 1
    return forgotten
