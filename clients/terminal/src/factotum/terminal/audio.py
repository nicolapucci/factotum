"""Real audio for the terminal client's ``--voice``: a microphone that waits for a wake word and
records what follows, and a speaker that plays what it is given.

Everything below the two protocols needs real hardware and is exercised by hand, never by the unit
tests: a test gives a session a fake instead (``factotum.terminal.testing``). ``sounddevice`` and
``pvporcupine`` are imported only once a ``MicrophoneInput`` or ``SpeakerOutput`` is actually built,
so running the terminal client without ``--voice`` never needs them installed.
"""

import asyncio
import io
import wave
from collections.abc import AsyncIterator
from typing import Any, Protocol

from factotum.contracts.audio import AudioFormat, AudioPayload

DEFAULT_WAKE_WORD = "jarvis"
"""One of Porcupine's built-in keywords (``pvporcupine.KEYWORDS``); there is no "factotum" one."""

ACCESS_KEY_VARIABLE = "PICOVOICE_ACCESS_KEY"
"""A free key from https://console.picovoice.ai/: Porcupine needs one even for a built-in
keyword."""


class VoiceInput(Protocol):
    def listen(self) -> AsyncIterator[AudioPayload]:
        """One recording each time the wake word is heard and the person has stopped talking.

        A future slice streams audio instead of waiting for silence to end a recording, and lets a
        wake word heard while an answer is playing interrupt it (ADR 0021); this is a single clip
        at a time, like the edge's ``audio_utterance`` is.
        """
        ...


class VoiceOutput(Protocol):
    async def play(self, audio: AudioPayload) -> None: ...


class MicrophoneInput:
    """A real microphone, watched by Porcupine. Once it fires, what follows is recorded until
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

    async def listen(self) -> AsyncIterator[AudioPayload]:
        import pvporcupine
        import sounddevice as sd  # heavy, and needs real hardware: deferred to the first use

        porcupine = await asyncio.to_thread(
            pvporcupine.create, access_key=self._access_key, keywords=[self._wake_word]
        )
        try:
            sample_rate, frame_length = porcupine.sample_rate, porcupine.frame_length
            loop = asyncio.get_running_loop()
            chunks: asyncio.Queue[Any] = asyncio.Queue()

            def on_audio(indata: Any, frames: int, time: Any, status: Any) -> None:
                loop.call_soon_threadsafe(chunks.put_nowait, indata[:, 0].copy())

            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=frame_length,
                callback=on_audio,
            ):
                while True:
                    chunk = await chunks.get()
                    if await asyncio.to_thread(porcupine.process, chunk) < 0:
                        continue
                    yield await self._record(chunks, sample_rate)
        finally:
            porcupine.delete()

    async def _record(self, chunks: "asyncio.Queue[Any]", sample_rate: int) -> AudioPayload:
        """What is said after the wake word, as one clip."""
        import numpy as np

        collected: list[Any] = []
        quiet_chunks = 0
        first = await chunks.get()
        collected.append(first)
        chunk_seconds = len(first) / sample_rate
        needed_quiet = max(1, int(self._silence_seconds / chunk_seconds))
        max_chunks = max(1, int(self._max_seconds / chunk_seconds))
        for _ in range(max_chunks - 1):
            chunk = await chunks.get()
            collected.append(chunk)
            if np.abs(chunk).mean() < self._silence_level:
                quiet_chunks += 1
                if quiet_chunks >= needed_quiet:
                    break
            else:
                quiet_chunks = 0
        samples = np.concatenate(collected).astype(np.int16)
        return _wav_of(samples, sample_rate)


class SpeakerOutput:
    """A real speaker."""

    async def play(self, audio: AudioPayload) -> None:
        import sounddevice as sd  # heavy, and needs real hardware: deferred to the first use

        samples = _pcm16_of(audio)
        await asyncio.to_thread(sd.play, samples, samplerate=audio.sample_rate, blocking=True)


def _wav_of(samples: Any, sample_rate: int) -> AudioPayload:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())
    return AudioPayload(format=AudioFormat.WAV, sample_rate=sample_rate, data=buffer.getvalue())


def _pcm16_of(audio: AudioPayload) -> Any:
    import numpy as np

    with wave.open(io.BytesIO(audio.data)) as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(frames, dtype=np.int16)
