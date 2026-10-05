"""Audio for a client: what a person says as a stream of chunks, and the places it is played.

A device sends the edge what a person says as it hears it (``audio_start``, a chunk for each piece,
``audio_end``: ADR 0022), so what supplies it is an ``AudioStream``: the rate of the samples and the
samples as they come, until the person has stopped. Where they come from is all that tells a real
microphone from a file: the client that will be on a Raspberry Pi and the one that tests the whole
stack send the same frames.

What the assistant says comes back as the sound of each sentence, in one of two ways that a
device chooses between when it says hello (ADR 0023): a clip in each ``speech`` part, which a
``VoiceOutput`` plays whole, or a stream of chunks as the voice makes them, which a ``VoiceSink`` is
given. A speaker that can do the second does it, and says so.

Nothing here needs hardware or an audio library: they are the shapes that the hardware of a device
(``sounddevice`` on a PC and on a Raspberry Pi, files for a test) is made to fit.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from factotum.contracts.audio import MAX_CHUNK_BYTES, AudioPayload

DEFAULT_CHUNK_MS = 100
"""How much of what a person says is in each chunk: a device sends 100 to 200 ms at a time."""


@dataclass(frozen=True, slots=True)
class AudioStream:
    """What a person is saying, as a microphone hands it over: the rate of the samples (16-bit
    little-endian, one channel) and the samples as they come, a chunk at a time, until the person
    has stopped talking. Whoever takes it reads ``chunks`` to the end, or closes it."""

    sample_rate: int
    chunks: AsyncGenerator[bytes]

    @classmethod
    def of(
        cls,
        pcm: bytes,
        sample_rate: int,
        *,
        chunk_ms: int = DEFAULT_CHUNK_MS,
        realtime: bool = False,
    ) -> "AudioStream":
        """Samples that were recorded, handed over a chunk at a time: as fast as they are taken, or
        at the pace of a microphone (each chunk after the time it takes to say it)."""
        return cls(sample_rate, _cut(pcm, sample_rate, chunk_ms, realtime))


async def _cut(
    pcm: bytes, sample_rate: int, chunk_ms: int, realtime: bool
) -> AsyncGenerator[bytes]:
    # Whole samples (two bytes), and never more than a chunk may carry.
    size = min(max(2, sample_rate * 2 * chunk_ms // 1000 // 2 * 2), MAX_CHUNK_BYTES)
    for start in range(0, len(pcm), size):
        chunk = pcm[start : start + size]
        if realtime:
            await asyncio.sleep(len(chunk) / 2 / sample_rate)
        yield chunk


class VoiceInput(Protocol):
    def listen(self) -> AsyncIterator[AudioPayload | AudioStream]:
        """What a person says, each time they say it: a whole recording once they have stopped, or
        a stream that is begun as soon as they start (ADR 0022: the way the real device does it)."""
        ...


class VoiceOutput(Protocol):
    """A speaker that plays a clip whole: what a device that does not take a voice as a stream is
    sent in each ``speech`` part."""

    async def play(self, audio: AudioPayload) -> None:
        """Play ``audio`` and return when it has been played."""
        ...


class VoiceSink(Protocol):
    """A speaker that plays the sound of an answer as it arrives (ADR 0023).

    The sound of an answer is a track: ``begin`` at the rate of its samples, a ``write`` for each
    chunk, and ``finish`` when there is no more. A device that is spoken over, or told to stop,
    gives up on a track with ``abort``. What is written is what has been received and not what
    has been played: it arrives faster than it plays, so ``write`` may wait for the speaker to
    take it, and a speaker that cannot hold what it is given is a speaker that waits here. ``abort``
    may be called while a ``write`` is waiting.
    """

    async def begin(self, sample_rate: int) -> None:
        """A track begins: 16-bit little-endian samples, one channel, at ``sample_rate``."""
        ...

    async def write(self, pcm: bytes) -> None:
        """The next samples of the track, whole samples."""
        ...

    async def finish(self) -> None:
        """There is no more of the track: return when all of it has been played."""
        ...

    async def abort(self) -> None:
        """Stop now, and drop what has not been played."""
        ...


@runtime_checkable
class MarkedSink(VoiceSink, Protocol):
    """A speaker that wants to know where each sentence begins: a screen that follows the voice, a
    test that reads back what was said sentence by sentence. The sound of an answer is one track,
    and what a sentence of it is is what the edge says (``part_seq``): before the first samples of
    each, ``mark`` says which."""

    async def mark(self, part_seq: int) -> None:
        """What follows is the sound of the ``speech`` part whose ``seq`` is ``part_seq``."""
        ...
