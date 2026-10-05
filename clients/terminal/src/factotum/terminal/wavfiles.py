"""Audio in files, for a run of the terminal with no hardware: ``--wav-in`` and ``--wav-out``.

A recording on disk is said to the device as if a person had said it after the wake word, in the
same chunks a microphone would hand over, so it is the client that tests the whole stack: it sends
what the real one sends, and differs from it in where the sound comes from and in nothing else
(ADR 0022). What the assistant says is written to a file instead of being played.
"""

import wave
from collections.abc import AsyncIterator
from pathlib import Path

from factotum.client_core.audio import DEFAULT_CHUNK_MS, AudioStream
from factotum.contracts.audio import MAX_SAMPLE_RATE, MIN_SAMPLE_RATE, AudioPayload, pcm_of


class WavFileInput:
    """Recordings in WAV files (16-bit mono PCM), one utterance each, said in the order given.
    They are read when this is made, so that a file that is not one is known before the edge is
    reached, and what is said ends when the last of them has been."""

    def __init__(
        self, *paths: Path, chunk_ms: int = DEFAULT_CHUNK_MS, realtime: bool = False
    ) -> None:
        self._utterances = [self._read(path) for path in paths]
        self._chunk_ms = chunk_ms
        self._realtime = realtime

    @staticmethod
    def _read(path: Path) -> tuple[bytes, int]:
        try:
            pcm, sample_rate = pcm_of(path.read_bytes())
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from None
        if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
            raise ValueError(
                f"{path}: {sample_rate} Hz is not a rate that is streamed "
                f"({MIN_SAMPLE_RATE} to {MAX_SAMPLE_RATE} Hz)"
            )
        return pcm, sample_rate

    async def listen(self) -> AsyncIterator[AudioStream]:
        for pcm, sample_rate in self._utterances:
            yield AudioStream.of(pcm, sample_rate, chunk_ms=self._chunk_ms, realtime=self._realtime)


class WavFileOutput:
    """A speaker that writes what it is asked to play into a WAV file, one clip or track after the
    other: it plays a clip whole (``VoiceOutput``) and takes a track of sound as it arrives
    (``VoiceSink``, ADR 0023), so it is what the client that tests the whole stack speaks to either
    way. The file is whole after each clip and each track, and after a track that is given up on,
    so that what was said is there however the run ends."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rate: int | None = None
        self._pcm = bytearray()

    @property
    def seconds(self) -> float:
        """How much was said."""
        return len(self._pcm) / 2 / self._rate if self._rate else 0.0

    def _one_rate(self, rate: int, what: str) -> None:
        if self._rate not in (None, rate):
            raise ValueError(
                f"a file holds one rate: {self._rate} Hz, and this {what} is {rate} Hz"
            )
        self._rate = rate

    def _write(self) -> None:
        assert self._rate is not None  # noqa: S101 - only called once something was given
        with wave.open(str(self.path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._rate)
            wav_file.writeframes(bytes(self._pcm))

    # a clip, whole
    async def play(self, audio: AudioPayload) -> None:
        pcm, rate = pcm_of(audio.data)
        self._one_rate(rate, "clip")
        self._pcm.extend(pcm)
        self._write()

    # a track, as it arrives
    async def begin(self, sample_rate: int) -> None:
        self._one_rate(sample_rate, "track")

    async def write(self, pcm: bytes) -> None:
        self._pcm.extend(pcm)

    async def finish(self) -> None:
        if self._rate is not None:
            self._write()

    async def abort(self) -> None:
        if self._rate is not None:
            self._write()  # what was given before the person spoke over it was said
