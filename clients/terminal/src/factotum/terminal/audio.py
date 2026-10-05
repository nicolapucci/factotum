"""Audio for the terminal client: what a person says as a stream of chunks, and what is played.

A device sends the edge what a person says as it hears it (``audio_start``, a chunk for each piece,
``audio_end``: ADR 0022), so what supplies it is an ``AudioStream``: the rate of the samples and the
samples as they come, until the person has stopped. Where they come from is all that tells a real
microphone from a file (``factotum.terminal.wavfiles``): the client that will be on a Raspberry Pi
and the one that tests the whole stack send the same frames.

Everything below the protocols that needs real hardware is exercised by hand, never by the unit
tests: a test gives a session a fake instead (``factotum.terminal.testing``), and the logic of
where speech ends is a function that a test gives frames to (``capture_speech``). ``sounddevice``
and ``pvporcupine`` are imported only once a ``MicrophoneInput`` or ``SpeakerOutput`` is actually
built, so running the terminal client without ``--voice`` never needs them installed.
"""

import asyncio
import io
import wave
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from factotum.contracts.audio import MAX_CHUNK_BYTES, AudioPayload

DEFAULT_WAKE_WORD = "jarvis"
"""One of Porcupine's built-in keywords (``pvporcupine.KEYWORDS``); there is no "factotum" one."""

ACCESS_KEY_VARIABLE = "PICOVOICE_ACCESS_KEY"
"""A free key from https://console.picovoice.ai/: Porcupine needs one even for a built-in
keyword."""

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
    async def play(self, audio: AudioPayload) -> None: ...


async def capture_speech(
    frames: "asyncio.Queue[Any]",
    sample_rate: int,
    *,
    silence_seconds: float,
    silence_level: float,
    max_seconds: float,
) -> AsyncGenerator[bytes]:
    """What is said after the wake word, as chunks of ``DEFAULT_CHUNK_MS`` taken from ``frames``
    (arrays of 16-bit samples, as a microphone hands them over) until the person has been quiet for
    ``silence_seconds`` or has talked for ``max_seconds``, whichever comes first. A frame that is
    quiet is one whose mean level is under ``silence_level``; the silence that ended it is in the
    last chunk, and what is left in ``frames`` after it is not taken."""
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
        over = quiet >= needed_quiet or taken >= max_frames
        if over or pending_seconds >= DEFAULT_CHUNK_MS / 1000:
            yield np.concatenate(pending).astype("<i2").tobytes()
            pending, pending_seconds = [], 0.0
        if over:
            return


class MicrophoneInput:
    """A real microphone, watched by Porcupine. Once it fires, what follows is streamed until
    ``silence_seconds`` of quiet or ``max_seconds`` have passed, whichever comes first.

    The first call validates ``access_key`` against Picovoice's service (a network call); the
    keyword model itself ships inside the ``pvporcupine`` package, so nothing is downloaded.
    """

    def __init__(
        self,
        *,
        access_key: str,
        wake_word: str = DEFAULT_WAKE_WORD,
        max_seconds: float = 12.0,
        silence_seconds: float = 1.2,
        silence_level: float = 150,
    ) -> None:
        self._access_key = access_key
        self._wake_word = wake_word
        self._max_seconds = max_seconds
        self._silence_seconds = silence_seconds
        self._silence_level = silence_level

    async def listen(self) -> AsyncIterator[AudioStream]:
        import pvporcupine
        import sounddevice as sd  # heavy, and needs real hardware: deferred to the first use

        porcupine = await asyncio.to_thread(
            pvporcupine.create, access_key=self._access_key, keywords=[self._wake_word]
        )
        try:
            sample_rate, frame_length = porcupine.sample_rate, porcupine.frame_length
            loop = asyncio.get_running_loop()
            frames: asyncio.Queue[Any] = asyncio.Queue()

            def on_audio(indata: Any, count: int, time: Any, status: Any) -> None:
                loop.call_soon_threadsafe(frames.put_nowait, indata[:, 0].copy())

            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=frame_length,
                callback=on_audio,
            ):
                while True:
                    frame = await frames.get()
                    if await asyncio.to_thread(porcupine.process, frame) < 0:
                        continue
                    speech = capture_speech(
                        frames,
                        sample_rate,
                        silence_seconds=self._silence_seconds,
                        silence_level=self._silence_level,
                        max_seconds=self._max_seconds,
                    )
                    try:
                        yield AudioStream(sample_rate, speech)
                    finally:
                        await speech.aclose()  # whoever took it may have stopped reading it
        finally:
            porcupine.delete()


class SpeakerOutput:
    """A real speaker."""

    async def play(self, audio: AudioPayload) -> None:
        import sounddevice as sd  # heavy, and needs real hardware: deferred to the first use

        samples = _pcm16_of(audio)
        await asyncio.to_thread(sd.play, samples, samplerate=audio.sample_rate, blocking=True)


def _pcm16_of(audio: AudioPayload) -> Any:
    import numpy as np

    with wave.open(io.BytesIO(audio.data)) as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(frames, dtype=np.int16)
