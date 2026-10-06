"""Where a person's speech ends: what is said after they have begun, as chunks for the edge, until
they have been quiet for a while, have talked for as long as they may, or have said they are done.

This is a function that is given frames the way a microphone hands them over, and the tests give it
frames (``tests/test_client_audio_endpoint.py``): it is the part of listening that decides things.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

from factotum.client_core.audio import DEFAULT_CHUNK_MS


async def capture_speech(
    frames: "asyncio.Queue[Any]",
    sample_rate: int,
    *,
    silence_seconds: float,
    silence_level: float,
    max_seconds: float,
    stop: Callable[[], bool] | None = None,
) -> AsyncGenerator[bytes]:
    """What is said, as chunks of ``DEFAULT_CHUNK_MS`` taken from ``frames`` (arrays of 16-bit
    samples, as a microphone hands them over) until the person has been quiet for
    ``silence_seconds``, has talked for ``max_seconds``, or ``stop()`` says so, whichever comes
    first. A frame that is quiet is one whose mean level is under ``silence_level``; the silence
    that ended it is in the last chunk, and what is left in ``frames`` after it is not taken.

    ``stop`` is asked after each frame is taken, and a person who presses a key to say that they are
    done is what it is for."""
    import numpy as np

    pending: list[Any] = []
    pending_seconds = 0.0
    quiet = taken = 0
    needed_quiet = max_frames = 0
    while True:
        frame = await frames.get()
        taken += 1
        frame_seconds = len(frame) / sample_rate
        if taken == 1:
            needed_quiet = max(1, int(silence_seconds / frame_seconds))
            max_frames = max(1, int(max_seconds / frame_seconds))
        quiet = quiet + 1 if np.abs(frame).mean() < silence_level else 0
        pending.append(frame)
        pending_seconds += frame_seconds
        over = quiet >= needed_quiet or taken >= max_frames or (stop is not None and stop())
        if over or pending_seconds >= DEFAULT_CHUNK_MS / 1000:
            yield np.concatenate(pending).astype("<i2").tobytes()
            pending, pending_seconds = [], 0.0
        if over:
            return
