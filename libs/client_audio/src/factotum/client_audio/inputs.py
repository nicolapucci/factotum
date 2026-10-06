"""The two ways a person gets a device to listen: a key, and a word.

Both are a ``factotum.client_core.VoiceInput``: each time a person asks to be heard they hand over
what they say as a stream, as they say it (ADR 0022), and the client does the rest. How a device
learns that somebody is about to speak is all that tells them apart, so a device is given one or the
other, and the rest of the client is the same.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Protocol

from factotum.client_audio.backend import AudioBackend, DeviceChoice
from factotum.client_audio.endpoint import capture_speech
from factotum.client_audio.microphone import Microphone, drain
from factotum.client_audio.wakeword import WakeWordEngine
from factotum.client_core.audio import AudioStream


class Presses(Protocol):
    """A person asking to speak, as often as they do."""

    def __aiter__(self) -> AsyncIterator[None]: ...

    def pending(self) -> bool:
        """Whether they asked again since the last time this was asked: they are saying they have
        done."""
        ...


class PushToTalk:
    """A key. Each press opens the microphone and what follows is streamed until the person has
    been quiet for ``silence_seconds``, has talked for ``max_seconds``, or presses the key again.
    The microphone is open for that and no longer: nothing is heard that was not asked to be."""

    def __init__(
        self,
        presses: Presses,
        microphone: Microphone,
        *,
        silence_seconds: float = 1.2,
        silence_level: float = 150,
        max_seconds: float = 12.0,
        on_listening: Callable[[], None] | None = None,
    ) -> None:
        self._presses = presses
        self._microphone = microphone
        self._silence_seconds = silence_seconds
        self._silence_level = silence_level
        self._max_seconds = max_seconds
        self._on_listening = on_listening

    async def listen(self) -> AsyncGenerator[AudioStream]:
        async for _ in self._presses:
            self._presses.pending()  # a press that came with this one is not a second thing to say
            async with self._microphone.opened() as blocks:
                if self._on_listening is not None:
                    self._on_listening()
                speech = capture_speech(
                    blocks,
                    self._microphone.sample_rate,
                    silence_seconds=self._silence_seconds,
                    silence_level=self._silence_level,
                    max_seconds=self._max_seconds,
                    stop=self._presses.pending,
                )
                try:
                    yield AudioStream(self._microphone.sample_rate, speech)
                finally:
                    await speech.aclose()  # whoever took it may have stopped reading it
            self._presses.pending()  # nor is a press that ended it


class WakeWord:
    """A word, watched for by an engine (``factotum.client_audio.wakeword``). Once it is heard,
    what follows is streamed until ``silence_seconds`` of quiet or ``max_seconds`` have passed,
    whichever comes first, and the engine is made to forget what it heard before it is asked
    again: the word that it had just found, and the sentence after it, are not a word to look for.

    ``engine`` makes the engine the first time the word is listened for: it may load models, or ask
    a service to validate a key, and is run where the loop does not wait for it.
    """

    def __init__(
        self,
        backend: AudioBackend,
        *,
        engine: Callable[[], WakeWordEngine],
        device: DeviceChoice = None,
        silence_seconds: float = 1.2,
        silence_level: float = 150,
        max_seconds: float = 12.0,
        on_listening: Callable[[], None] | None = None,
    ) -> None:
        self._backend = backend
        self._engine = engine
        self._device = device
        self._silence_seconds = silence_seconds
        self._silence_level = silence_level
        self._max_seconds = max_seconds
        self._on_listening = on_listening

    async def listen(self) -> AsyncGenerator[AudioStream]:
        engine = await asyncio.to_thread(self._engine)
        try:
            microphone = Microphone(
                self._backend,
                device=self._device,
                sample_rate=engine.sample_rate,
                block_size=engine.frame_length,
            )
            async with microphone.opened() as blocks:
                while True:
                    block = await blocks.get()
                    if not await asyncio.to_thread(engine.process, block):
                        continue
                    if self._on_listening is not None:
                        self._on_listening()
                    speech = capture_speech(
                        blocks,
                        microphone.sample_rate,
                        silence_seconds=self._silence_seconds,
                        silence_level=self._silence_level,
                        max_seconds=self._max_seconds,
                    )
                    try:
                        yield AudioStream(microphone.sample_rate, speech)
                    finally:
                        await speech.aclose()  # whoever took it may have stopped reading it
                    drain(blocks)  # what was heard while it was answered is not a word to look for
                    engine.reset()  # nor is what the engine heard before it, which it still holds
        finally:
            engine.close()
