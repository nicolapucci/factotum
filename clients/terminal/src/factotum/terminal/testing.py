"""Stand-ins for what the client depends on, so that the tests of everything above them need no
network: a connection in memory, whose other end a test plays, a renderer that writes down what it
was asked to show, and a person who types."""

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from typing import Any

from factotum.contracts.audio import AudioPayload
from factotum.contracts.base import Contract
from factotum.contracts.cards import Card
from factotum.contracts.edge import ErrorFrame, TurnEnded, decode_device_frame, encode_frame
from factotum.contracts.response import StatusState
from factotum.terminal.audio import AudioStream
from factotum.terminal.connection import ConnectionLost


class MemoryConnection:
    """The device's end of a connection to an edge that is played by a test: what the edge says is
    put in with ``say``, and what the device sent is in ``sent``."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[str | ConnectionLost] = asyncio.Queue()
        self._sent = asyncio.Event()
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionLost(None, "closed")
        self.sent.append(text)
        self._sent.set()

    async def recv(self) -> str:
        item = await self._incoming.get()
        if isinstance(item, ConnectionLost):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait(ConnectionLost(1000, ""))

    # ---- the edge's side
    def say(self, frame: Contract | str) -> None:
        self._incoming.put_nowait(frame if isinstance(frame, str) else encode_frame(frame))

    def drop(self, code: int | None = 1006, reason: str = "") -> None:
        """The connection ends: the edge closed it, or it was lost."""
        self._incoming.put_nowait(ConnectionLost(code, reason))

    def frames(self, *kinds: str) -> list[Any]:
        """What the device sent, decoded."""
        found = [decode_device_frame(text) for text in self.sent]
        return [f for f in found if not kinds or f.type in kinds]

    async def wait_for(self, kind: str, *, count: int = 1, timeout_s: float = 2.0) -> list[Any]:
        async with asyncio.timeout(timeout_s):
            while len(found := self.frames(kind)) < count:
                self._sent.clear()
                await self._sent.wait()
        return found


class RecordingRenderer:
    """Writes down what was shown, as ``(kind, what)`` pairs."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, Any]] = []

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.shown]

    def of(self, kind: str) -> list[Any]:
        return [what for k, what in self.shown if k == kind]

    def speech(self, text: str) -> None:
        self.shown.append(("speech", text))

    def text(self, text: str) -> None:
        self.shown.append(("text", text))

    def card(self, card: Card) -> None:
        self.shown.append(("card", card))

    def media(self, title: str | None, url: str) -> None:
        self.shown.append(("media", (title, url)))

    def status(self, state: StatusState) -> None:
        self.shown.append(("status", state))

    def event(self, what: str) -> None:
        self.shown.append(("event", what))

    def heard(self, text: str) -> None:
        self.shown.append(("heard", text))

    def turn_ended(self, ended: TurnEnded) -> None:
        self.shown.append(("turn_ended", ended))

    def error(self, error: ErrorFrame) -> None:
        self.shown.append(("error", error))

    def violation(self, what: str) -> None:
        self.shown.append(("violation", what))

    def notice(self, what: str) -> None:
        self.shown.append(("notice", what))


async def never() -> None:
    """What a person does who has nothing more to type: wait, for ever."""
    await asyncio.Event().wait()


class FakeVoiceInput:
    """A microphone that has already heard what it is given, one recording (or one stream) per
    wake word; after the last of them, no further wake word is ever heard."""

    def __init__(self, *heard: AudioPayload | AudioStream) -> None:
        self._heard = list(heard)

    async def listen(self) -> AsyncIterator[AudioPayload | AudioStream]:
        for what in self._heard:
            yield what
        await never()


class FakeVoiceOutput:
    """A speaker that remembers what it was asked to play."""

    def __init__(self) -> None:
        self.played: list[AudioPayload] = []

    async def play(self, audio: AudioPayload) -> None:
        self.played.append(audio)


class Typed:
    """What a person types, a line at a time. Where a line is a function it is run when it is
    reached (and awaited, if it is asynchronous), which is how the edge is made to say something in
    between."""

    def __init__(self, *lines: str | Callable[[], Any]) -> None:
        self._lines = lines

    async def read(self) -> AsyncIterator[str | None]:
        for line in self._lines:
            if isinstance(line, str):
                yield line
            elif inspect.isawaitable(waiting := line()):
                await waiting
            # The terminal is given the time to do what it has been told, as it would be by the
            # seconds a person takes to type.
            await asyncio.sleep(0.01)
        yield None
