"""A speaker: it plays a clip whole, or the voice of an answer as it arrives, at its own volume.

The edge sends the voice faster than it plays (ADR 0023) and does not pace it, so the speaker is
what paces it: a ``write`` waits until the device has room for the samples, and the client, which is
reading the connection while this waits, never waits for the sound. Telling a speaker to stop
(``abort``) lets a ``write`` that is waiting go, and that is what a person speaking over the answer
gets: the sound ends where it was, and not where the device had got to in what it had been given.

The volume is the speaker's and not the system's: a ``set_volume`` or an ``adjust_volume`` command
scales the samples before they are played, so that the same device does it on a PC, on a Raspberry
Pi and on whatever has no mixer, and a person's own setting of the system's volume is left alone.
How far "turn it up" goes when nobody says is the speaker's own step (ADR 0028).
"""

import asyncio

from factotum.client_audio.backend import (
    AudioBackend,
    AudioUnavailableError,
    DeviceChoice,
    OutputStream,
)
from factotum.contracts.audio import AudioPayload, pcm_of
from factotum.contracts.commands import VolumeDirection

FULL_VOLUME = 100
"""The level of a speaker that changes nothing of what it is given."""

DEFAULT_VOLUME_STEP = 10
"""How far a speaker turns the volume when nobody says by how much: the step of the device, which
its settings may change (``audio.volume_step`` of the device application)."""

CLIP_CHUNK_MS = 100
"""A clip is written a tenth of a second at a time, so that stopping it does not wait for the
whole of it to be taken by the device."""


def gain_of(level: int) -> float:
    """What a volume does to the amplitude of the samples: the square of the share of the full
    volume, since a person hears the loudness of what is played and not its amplitude, and a volume
    at half that is half as loud to them would change very little."""
    return (max(0, min(level, FULL_VOLUME)) / FULL_VOLUME) ** 2


class Speaker:
    """A speaker of the system (``device``: its index, a part of its name, or None for the
    default), as a ``factotum.client_core.VoiceOutput`` (a clip), a ``VoiceSink`` (a track as it
    arrives) and a ``MarkedSink`` (told which sentence it is saying)."""

    def __init__(
        self,
        backend: AudioBackend,
        *,
        device: DeviceChoice = None,
        volume: int = FULL_VOLUME,
        step: int = DEFAULT_VOLUME_STEP,
    ) -> None:
        self._backend = backend
        self._device = device
        self._volume = max(0, min(volume, FULL_VOLUME))
        self._step = max(1, min(step, FULL_VOLUME))
        self._stream: OutputStream | None = None
        self.sentence: int | None = None
        """The ``seq`` of the sentence that is being said, as the last ``mark`` told it."""

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def step(self) -> int:
        """How far this speaker turns the volume when nobody says by how much."""
        return self._step

    def set_volume(self, level: int) -> None:
        self._volume = max(0, min(level, FULL_VOLUME))

    def adjust_volume(self, direction: VolumeDirection, amount: int | None = None) -> int:
        """Louder or quieter by ``amount`` points of the volume, or by the step of this speaker when
        there is none, and never past either end. Returns the level that results."""
        points = self._step if amount is None else amount
        self.set_volume(self._volume + (points if direction is VolumeDirection.UP else -points))
        return self._volume

    def scaled(self, pcm: bytes) -> bytes:
        """The samples at the volume of the speaker."""
        gain = gain_of(self._volume)
        if gain == 1.0:
            return pcm
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) * gain
        return np.clip(samples, -32768, 32767).astype("<i2").tobytes()

    # ---- a clip ------------------------------------------------------------------------------

    async def play(self, audio: AudioPayload) -> None:
        """Play ``audio`` and return when it has been played."""
        pcm, sample_rate = pcm_of(audio.data)
        size = max(2, sample_rate * 2 * CLIP_CHUNK_MS // 1000 // 2 * 2)
        await self.begin(sample_rate)
        try:
            for start in range(0, len(pcm), size):
                await self.write(pcm[start : start + size])
            await self.finish()
        except BaseException:
            await self.abort()
            raise

    # ---- a track -----------------------------------------------------------------------------

    async def begin(self, sample_rate: int) -> None:
        """A track begins. A track that was never ended is let go of."""
        await self.abort()
        self.sentence = None
        self._stream = await asyncio.to_thread(self._backend.open_output, self._device, sample_rate)

    async def write(self, pcm: bytes) -> None:
        """The next samples: this returns when the device has taken them."""
        stream = self._stream
        if stream is None:
            raise AudioUnavailableError("a track was not begun")
        await asyncio.to_thread(stream.write, self.scaled(pcm))

    async def finish(self) -> None:
        """There is no more of the track: this returns when all of it has been played."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            await asyncio.to_thread(stream.stop)
        finally:
            await asyncio.to_thread(stream.close)
            self.sentence = None

    async def abort(self) -> None:
        """Stop now: what was written and not played is dropped, and a ``write`` that is waiting
        goes."""
        stream, self._stream = self._stream, None
        self.sentence = None
        if stream is None:
            return
        try:
            await asyncio.to_thread(stream.abort)
        finally:
            await asyncio.to_thread(stream.close)

    async def mark(self, part_seq: int) -> None:
        """What follows is the sound of the sentence whose ``seq`` is ``part_seq``."""
        self.sentence = part_seq
