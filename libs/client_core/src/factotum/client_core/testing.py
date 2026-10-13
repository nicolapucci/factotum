"""Stand-ins for what a client depends on, so that the tests of everything above them need no
network and no hardware: a connection in memory, whose other end a test plays, a renderer that
writes down what it was asked to show, a person who has already said what they are going to say, and
speakers that remember what they were made to play."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from factotum.client_core.audio import AudioStream, VoiceOutput, VoiceSink
from factotum.client_core.client import DeviceClient
from factotum.client_core.connection import ConnectionLost
from factotum.client_core.ports import CommandRouter, DeviceIdentity
from factotum.client_core.timekeeping import Timekeeper
from factotum.contracts.audio import AudioPayload, wav_of
from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.cards import Card
from factotum.contracts.commands import CommandResult, DeviceCommand
from factotum.contracts.edge import (
    PROTOCOL_VERSION,
    ErrorFrame,
    Ready,
    TurnEnded,
    decode_device_frame,
    encode_frame,
)
from factotum.contracts.ids import DeviceId
from factotum.contracts.response import StatusState

SPEAKER_CAPABILITIES = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.LED})
"""What a speaker with a microphone and a light can do."""

SCREEN = DisplayInfo(width=800, height=480, touch=True)


def a_device(
    capabilities: frozenset[Capability] = SPEAKER_CAPABILITIES,
    *,
    hardware_id: str = "kitchen-01",
    token: str = "kitchen-credential",  # noqa: S107 - what a test says it is
    display: DisplayInfo | None = None,
) -> DeviceIdentity:
    """Who a device is, for a test of a client."""
    return DeviceIdentity(
        hardware_id=hardware_id, token=token, capabilities=capabilities, display=display
    )


KINDS = {
    "speaker": SPEAKER_CAPABILITIES,
    "smart-display": frozenset(
        {Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.DISPLAY, Capability.LED}
    ),
    "screen": frozenset({Capability.DISPLAY}),
    "player": frozenset({Capability.AUDIO_OUT, Capability.MEDIA_PLAYER}),
}
"""The kinds of device that the tests of a client are about: what a device of each has."""


def identity_of(kind: str) -> DeviceIdentity:
    """Who a device of this kind is."""
    capabilities = KINDS[kind]
    return a_device(
        capabilities,
        hardware_id=f"{kind}-01",
        token=f"{kind}-credential",
        display=SCREEN if Capability.DISPLAY in capabilities else None,
    )


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


class RecordingCommands:
    """The hands of a device that remember what they were told to do, and answer as they were
    told to answer (``results``, in order; a command that has none is done). They stand for
    ``capabilities`` (all, unless said): a ``CommandRouter`` gives them what needs one of them."""

    def __init__(
        self,
        *results: CommandResult,
        capabilities: frozenset[Capability] = frozenset(Capability),
    ) -> None:
        self.commands: list[DeviceCommand] = []
        self.capabilities = capabilities
        self._results = list(results)

    async def execute(self, command: DeviceCommand) -> CommandResult:
        self.commands.append(command)
        return self._results.pop(0) if self._results else CommandResult.ok()


async def never() -> None:
    """What a person does who has nothing more to say: wait, for ever."""
    await asyncio.Event().wait()


class FakeVoiceInput:
    """A microphone that has already heard what it is given, one recording (or one stream) per
    wake word; after the last of them, no further wake word is ever heard, unless ``ends``: then
    there is no more (the person has left, as when the files of ``--wav-in`` are all said)."""

    def __init__(self, *heard: AudioPayload | AudioStream, ends: bool = False) -> None:
        self._heard = list(heard)
        self._ends = ends

    async def listen(self) -> AsyncIterator[AudioPayload | AudioStream]:
        for what in self._heard:
            yield what
        if not self._ends:
            await never()


class FakeVoiceOutput:
    """A speaker that plays clips, and remembers what it was asked to play."""

    def __init__(self) -> None:
        self.played: list[AudioPayload] = []

    async def play(self, audio: AudioPayload) -> None:
        self.played.append(audio)


@dataclass
class Track:
    """What a speaker was given of one track of sound."""

    sample_rate: int
    pcm: bytearray = field(default_factory=bytearray)
    finished: bool = False
    aborted: bool = False
    sentences: list[tuple[int, bytearray]] = field(default_factory=list)
    """What it was given of each sentence, as ``(part_seq, samples)`` in the order they came."""

    def clip(self) -> AudioPayload:
        """What was given, as the clip that a recording of it would be."""
        return wav_of(bytes(self.pcm), self.sample_rate)


class FakeVoiceSink:
    """A speaker that plays the sound of an answer as it is given it, and remembers every track.

    ``gate``, when it is given and not set, makes a ``write`` wait for it: a speaker that takes its
    time. ``fails`` is raised by the next call that names it (``begin``, ``write``, ``finish``, as a
    set of names), once: a speaker that is unplugged."""

    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        self.tracks: list[Track] = []
        self.calls: list[str] = []
        """What it was told, in order: ``begin``, ``write``, ``finish``, ``abort``."""

        self.gate = gate
        self.fails: dict[str, Exception] = {}
        self.writing = asyncio.Event()
        """Set when a ``write`` has begun: for a test to know a speaker is busy."""

    @property
    def sound(self) -> bytes:
        """Everything that was given, of every track, one after the other."""
        return b"".join(bytes(track.pcm) for track in self.tracks)

    def _fail(self, call: str) -> None:
        if call in self.fails:
            raise self.fails.pop(call)

    async def begin(self, sample_rate: int) -> None:
        self.calls.append("begin")
        self._fail("begin")
        self.tracks.append(Track(sample_rate))

    async def write(self, pcm: bytes) -> None:
        self.calls.append("write")
        self.writing.set()
        if self.gate is not None:
            await self.gate.wait()
        self._fail("write")
        track = self.tracks[-1]
        track.pcm.extend(pcm)
        if track.sentences:
            track.sentences[-1][1].extend(pcm)

    async def finish(self) -> None:
        self.calls.append("finish")
        self._fail("finish")
        self.tracks[-1].finished = True

    async def abort(self) -> None:
        self.calls.append("abort")
        if self.tracks:
            self.tracks[-1].aborted = True


class FakeMarkedSink(FakeVoiceSink):
    """A speaker that is told where each sentence begins (``MarkedSink``), and can say what it was
    given sentence by sentence."""

    async def mark(self, part_seq: int) -> None:
        self.calls.append("mark")
        self.tracks[-1].sentences.append((part_seq, bytearray()))

    def sentences(self) -> list[AudioPayload]:
        """What was given, a clip for each sentence, in order: what a clip in each ``speech`` part
        would have been, for a test that reads back what the speaker said."""
        return [
            wav_of(bytes(pcm), track.sample_rate)
            for track in self.tracks
            for _, pcm in track.sentences
            if pcm
        ]


class FakeSpeaker(FakeMarkedSink, FakeVoiceOutput):
    """A speaker that does both: plays a clip whole, and plays a track as it is given it."""

    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        FakeMarkedSink.__init__(self, gate=gate)
        FakeVoiceOutput.__init__(self)


def chunks_of(pcm: bytes, size: int) -> Sequence[bytes]:
    """``pcm`` in chunks of ``size`` bytes, the last whatever is left."""
    return [pcm[start : start + size] for start in range(0, len(pcm), size)]


async def settle() -> None:
    """Let the tasks that are ready do what they do, and the ones they make ready after them."""
    for _ in range(10):
        await asyncio.sleep(0)


class FakeClock:
    """The clock of a device, moved by a test: the wall (civil time) and the clock that only goes
    forward move together when time ``advance``s, whoever sleeps is woken in the order of when they
    were to wake, and each gets to do what it does before the next is woken. The wall alone can be
    set to another hour (``set_wall``), as it is when summer time begins or a device is moved."""

    def __init__(self, wall: datetime | None = None) -> None:
        self._wall = wall or datetime(2026, 10, 13, 22, 0, 0)  # noqa: DTZ001 - civil time
        self._monotonic = 1000.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def wall(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        entry = (
            self._monotonic + max(0.0, seconds),
            asyncio.get_running_loop().create_future(),
        )
        self._sleepers.append(entry)
        try:
            await entry[1]
        finally:
            if entry in self._sleepers:
                self._sleepers.remove(entry)

    def set_wall(self, wall: datetime) -> None:
        self._wall = wall

    @property
    def sleepers(self) -> int:
        """How many are asleep, waiting for time to pass."""
        return len(self._sleepers)

    async def advance(self, seconds: float) -> None:
        """Time passes. What was just made to wait is first given the chance to begin to, so that a
        task that is yet to look at the clock does not find the time already gone."""
        await settle()
        target = self._monotonic + seconds
        while True:
            due = [entry for entry in self._sleepers if entry[0] <= target]
            if not due:
                break
            entry = min(due, key=lambda sleeper: sleeper[0])
            self._sleepers.remove(entry)
            self._move(entry[0] - self._monotonic)
            if not entry[1].done():
                entry[1].set_result(None)
            await settle()
        self._move(target - self._monotonic)
        await settle()

    def _move(self, seconds: float) -> None:
        self._monotonic += seconds
        self._wall += timedelta(seconds=seconds)


class ClientRig:
    """A client with a fake for everything it is plugged into, and an edge that a test plays by
    saying frames on ``connection``. Its device has a clock that the test moves (``clock``) and a
    keeper of its timers and alarms (``timekeeper``), which a test may give instead."""

    def __init__(
        self,
        kind: str = "speaker",
        *,
        voice_output: VoiceOutput | None = None,
        voice_sink: VoiceSink | None = None,
        hands: "RecordingCommands | None" = None,
        connection: MemoryConnection | None = None,
        clock: FakeClock | None = None,
        timekeeper: Timekeeper | None = None,
    ) -> None:
        self.identity = identity_of(kind)
        self.connection = connection or MemoryConnection()
        self.shown = RecordingRenderer()
        self.hands = hands or RecordingCommands(capabilities=self.identity.capabilities)
        self.clock = clock or FakeClock()
        self.timekeeper = timekeeper or Timekeeper(
            self.shown, clock=self.clock, output=voice_output
        )
        self.client = DeviceClient(
            self.connection,
            self.identity,
            self.shown,
            CommandRouter(self.hands),
            voice_output=voice_output,
            voice_sink=voice_sink,
            timekeeper=self.timekeeper,
        )

    def ready(self, **changes: Any) -> Ready:
        """What the edge says when it knows the device."""
        fields: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": "s1",
            "device_id": DeviceId(uuid4()),
            "name": "Kitchen speaker",
            "capabilities": self.identity.capabilities,
        }
        return Ready(**{**fields, **changes})

    async def connected(self, **changes: Any) -> "ClientRig":
        self.connection.say(self.ready(**changes))
        await self.client.connect()
        return self

    async def running(self, **changes: Any) -> "asyncio.Task[Any]":
        """Connected, and doing what the edge sends."""
        await self.connected(**changes)
        return asyncio.create_task(self.client.run())
