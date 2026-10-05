"""What the edge needs of what is outside it: a device's connection, and the model gateway's speech.

They are the things a session is given and that tests stand in for (``factotum.edge.testing``); the
session and the voice of an answer both depend on them, and neither on the other.
"""

from collections.abc import AsyncGenerator
from typing import Protocol

from factotum.contracts.audio import AudioPayload
from factotum.contracts.identity import Owner
from factotum.contracts.speech import SynthesisFormat, SynthesisSamples


class Disconnected(Exception):  # noqa: N818 - it says what happened to the device, not what failed
    """The device is gone: its connection closed, or a frame could not be sent to it."""


class Wire(Protocol):
    """What a session needs of a connection: text frames in and out, and a way to end it."""

    async def receive(self) -> str:
        """The next frame. Raises ``Disconnected`` when the connection is over."""
        ...

    async def send(self, text: str) -> None:
        """Send a frame. Raises ``Disconnected`` when it cannot be sent."""
        ...

    async def close(self, code: int, reason: str = "") -> None: ...


class SpeechSource(Protocol):
    """What a session needs to turn a recording into words, and words into one, for the owner the
    principal acts as. ``None`` where a session is given none: a recording (an ``audio_utterance``,
    or a stream of audio) is then refused, and a ``Speech`` part goes out as text alone."""

    async def transcribe(self, owner: Owner, audio: AudioPayload) -> str: ...

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload: ...

    def synthesize_stream(
        self, owner: Owner, text: str
    ) -> AsyncGenerator[SynthesisFormat | SynthesisSamples]:
        """The sound of ``text`` as the voice makes it (ADR 0023): its format, then its samples. A
        failure is raised, typed, when it happens; a stream that ends is a voice that said it
        all."""
        ...
