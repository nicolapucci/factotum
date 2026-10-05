"""Stand-ins for what the edge depends on (a device's connection, the conversation service, the
registry of devices), so that the tests of everything above them need no network and no model."""

import asyncio
import socket
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any
from uuid import uuid4

import uvicorn

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevAuthenticator
from factotum.contracts.audio import AudioPayload
from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.conversation import (
    Citation,
    Cited,
    Delta,
    Finished,
    Phase,
    ReferenceKind,
    Started,
    Status,
    TurnEvent,
)
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.edge import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    Feature,
    Hello,
    Ready,
    decode_edge_frame,
    encode_frame,
)
from factotum.contracts.identity import Owner, Principal, Subject
from factotum.contracts.ids import CollectionId, ConversationId, DocumentId, ExchangeId
from factotum.contracts.speech import SynthesisFormat, SynthesisSamples
from factotum.contracts.testing import make_system_device, make_tenant_id, make_user_id
from factotum.edge.api import create_app
from factotum.edge.ports import Disconnected
from factotum.edge.registry import FileDeviceRegistry
from factotum.edge.router import Sessions
from factotum.edge.session import Session, SessionSettings

Frame = Any
"""A frame the edge sent, decoded (any of the models of ``factotum.contracts.edge``)."""


class FakeWire:
    """A device's end of a connection, as a session sees it: what the device says is put in with
    ``say``, and what the edge sent is in ``sent``."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self._sent = asyncio.Event()
        self.sent: list[str] = []
        self.closed_with: tuple[int, str] | None = None
        self.send_fails = False

    async def receive(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise Disconnected
        return item

    async def send(self, text: str) -> None:
        if self.closed_with is not None or self.send_fails:
            raise Disconnected
        self.sent.append(text)
        self._sent.set()

    async def close(self, code: int, reason: str = "") -> None:
        if self.closed_with is None:
            self.closed_with = (code, reason)
        self._incoming.put_nowait(None)  # what waits to hear from the device hears it is gone

    # ---- the device's side
    def say(self, frame: Contract | str) -> None:
        self._incoming.put_nowait(frame if isinstance(frame, str) else encode_frame(frame))

    def hang_up(self) -> None:
        self._incoming.put_nowait(None)

    def frames(self, *kinds: str) -> list[Frame]:
        found = [decode_edge_frame(text) for text in self.sent]
        return [f for f in found if not kinds or f.type in kinds]

    async def wait_for(self, kind: str, *, count: int = 1, timeout_s: float = 2.0) -> list[Frame]:
        """The frames of a kind, once at least ``count`` of them have been sent."""
        async with asyncio.timeout(timeout_s):
            while len(found := self.frames(kind)) < count:
                self._sent.clear()
                await self._sent.wait()
        return found


class Hang:
    """An answer that never goes on: what a model that has stopped looks like."""


HANG = Hang()


@dataclass(frozen=True, slots=True)
class Wait:
    seconds: float


Step = TurnEvent | Exception | Wait | Hang


def answer_events(
    *deltas: str,
    sources: Sequence[Citation] = (),
    reference: ReferenceKind = ReferenceKind.NONE,
) -> list[TurnEvent]:
    """The events of an answer that goes well: it began, it searched, it wrote ``deltas`` and, if
    it cited ``sources``, it said which."""
    events: list[TurnEvent] = [
        Started(conversation_id=ConversationId(uuid4()), exchange_id=ExchangeId(uuid4())),
        Status(phase=Phase.SEARCHING),
        Status(phase=Phase.ANSWERING),
        *(Delta(text=delta) for delta in deltas),
    ]
    if sources:
        events.append(Cited(citations=tuple(sources)))
    events.append(
        Finished(
            reference=reference,
            exchanges_used=0,
            passages_used=len(sources),
            untrusted_content=bool(sources),
        )
    )
    return events


def answer(
    *deltas: str,
    sources: Sequence[Citation] = (),
    reference: ReferenceKind = ReferenceKind.NONE,
) -> list[Step]:
    """The same, as a script for ``FakeConversation``."""
    return list(answer_events(*deltas, sources=sources, reference=reference))


def citation(number: int = 1, title: str = "Router manual", page: int | None = 12) -> Citation:
    return Citation(
        number=number,
        document_id=DocumentId(uuid4()),
        collection_id=CollectionId(uuid4()),
        document_title=title,
        page=page,
        headings=("Router", "Factory reset"),
    )


class FakeConversation:
    """A conversation service that answers as told, and keeps what it was asked.

    ``scripts`` are what it does for the next turns, in order: each a list of events to stream, an
    error to raise when it is reached, a ``Wait`` to let time pass or ``HANG`` to go no further.
    Without one it answers "An answer.".
    """

    def __init__(self) -> None:
        self.turns: list[tuple[Principal, str]] = []
        self.scripts: list[Sequence[Step]] = []
        self.ready_answer = True
        self.open = 0
        """Streams begun and not yet over."""

        self.abandoned = 0
        """Streams that were given up on before their end: the model behind is not kept writing."""

    def turn(self, principal: Principal, message: str) -> AsyncGenerator[TurnEvent]:
        return self._turn(principal, message)

    async def _turn(self, principal: Principal, message: str) -> AsyncGenerator[TurnEvent]:
        self.turns.append((principal, message))
        script = self.scripts.pop(0) if self.scripts else answer("An answer.")
        self.open += 1
        finished = False
        try:
            for step in script:
                if isinstance(step, Exception):
                    raise step
                if isinstance(step, Wait):
                    await asyncio.sleep(step.seconds)
                elif isinstance(step, Hang):
                    await asyncio.Event().wait()
                else:
                    yield step
            finished = True
        finally:
            self.open -= 1
            if not finished:
                self.abandoned += 1

    async def ready(self) -> bool:
        return self.ready_answer


DEFAULT_CLIP = AudioPayload(sample_rate=16000, data=b"spoken-aloud")
DEFAULT_SOUND = (SynthesisFormat(sample_rate=16000), SynthesisSamples(data=b"spoken-aloud"))
"""What the voice gives for a sentence that nothing was scripted for."""

DEFAULT_TRANSCRIPTION = "what was said"

VoiceStep = SynthesisFormat | SynthesisSamples | Exception | Wait | Hang | asyncio.Event


class FakeSpeech:
    """The model gateway's speech roles, answering as told. Without a script, ``transcribe``
    returns ``DEFAULT_TRANSCRIPTION`` and ``synthesize`` returns ``DEFAULT_CLIP``, so a test that
    only cares that *something* was heard or spoken need not script either. A transcription that is
    scripted as ``HANG`` never ends: a model that is still listening.

    The voice of a stream (``synthesize_stream``) gives ``DEFAULT_SOUND`` for a sentence, or what
    was scripted for it with ``will_stream``, and keeps how each stream ended."""

    def __init__(self) -> None:
        self.transcriptions: list[tuple[Owner, AudioPayload]] = []
        self.syntheses: list[tuple[Owner, str]] = []
        self.voices: list[tuple[Owner, str]] = []
        """The sentences a stream of sound was asked for."""

        self.open_voices = 0
        """Streams begun and not yet over."""

        self.endings: list[str] = []
        """How each stream ended: ``completed``, ``failed`` (it raised), ``cancelled`` (the task
        that read it was) or ``closed`` (whoever read it let it go before its end)."""

        self._transcribe_script: list[str | Exception | Hang] = []
        self._synthesize_script: list[AudioPayload | Exception] = []
        self._stream_script: list[Sequence[VoiceStep]] = []

    def will_transcribe(self, *outcomes: str | Exception | Hang) -> "FakeSpeech":
        self._transcribe_script.extend(outcomes)
        return self

    def will_synthesize(self, *outcomes: AudioPayload | Exception) -> "FakeSpeech":
        self._synthesize_script.extend(outcomes)
        return self

    def will_stream(self, *steps: VoiceStep) -> "FakeSpeech":
        """What the voice does for the next sentence it is asked for: the format and the samples
        it gives, an error to raise when it is reached, a ``Wait`` to let time pass, ``HANG`` to
        go no further or an ``asyncio.Event`` that it goes on when it is set."""
        self._stream_script.append(steps)
        return self

    async def transcribe(self, owner: Owner, audio: AudioPayload) -> str:
        self.transcriptions.append((owner, audio))
        outcome = (
            self._transcribe_script.pop(0) if self._transcribe_script else DEFAULT_TRANSCRIPTION
        )
        if isinstance(outcome, Hang):
            await asyncio.Event().wait()
            raise AssertionError("a transcription that hangs does not end")  # pragma: no cover
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        self.syntheses.append((owner, text))
        outcome = self._synthesize_script.pop(0) if self._synthesize_script else DEFAULT_CLIP
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def synthesize_stream(
        self, owner: Owner, text: str
    ) -> AsyncGenerator[SynthesisFormat | SynthesisSamples]:
        return self._stream(owner, text)

    async def _stream(
        self, owner: Owner, text: str
    ) -> AsyncGenerator[SynthesisFormat | SynthesisSamples]:
        self.voices.append((owner, text))
        script = self._stream_script.pop(0) if self._stream_script else DEFAULT_SOUND
        self.open_voices += 1
        ending = "closed"
        try:
            for step in script:
                if isinstance(step, Exception):
                    raise step
                if isinstance(step, Wait):
                    await asyncio.sleep(step.seconds)
                elif isinstance(step, Hang):
                    await asyncio.Event().wait()
                elif isinstance(step, asyncio.Event):
                    await step.wait()  # a gate: the voice goes on when the test says so
                else:
                    yield step
            ending = "completed"
        except asyncio.CancelledError:
            ending = "cancelled"
            raise
        except Exception:
            ending = "failed"
            raise
        finally:
            self.open_voices -= 1
            self.endings.append(ending)


TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)
TENANT = make_tenant_id()
USER = make_user_id()

SPEAKER = (Capability.AUDIO_IN, Capability.AUDIO_OUT)
KITCHEN_CREDENTIAL = "kitchen-credential"


def make_device(
    name: str = "Kitchen speaker",
    hardware_id: str = "kitchen-01",
    token: str = KITCHEN_CREDENTIAL,
    *,
    shared: bool = True,
    capabilities: Iterable[Capability] = SPEAKER,
    display: DisplayInfo | None = None,
) -> tuple[SystemDeviceRecord, str]:
    """A registered device, and the credential it was given."""
    subject = Subject.tenant(TENANT) if shared else Subject.user(USER)
    record = make_system_device(
        tenant_id=TENANT,
        owner=Owner.of_subject(subject),
        capabilities=capabilities,
        display=display,
        hardware_id=hardware_id,
        name=name,
    )
    return record, token


def hello(
    device: tuple[SystemDeviceRecord, str],
    *,
    token: str | None = None,
    capabilities: Iterable[Capability] | None = None,
    display: DisplayInfo | None = None,
    hardware_id: str | None = None,
    protocol_version: int = PROTOCOL_VERSION,
    features: Iterable[Feature] = (),
) -> Hello:
    """What a device says first: by default what it is registered as, and its credential."""
    record, issued = device
    return Hello(
        protocol_version=protocol_version,
        hardware_id=hardware_id or record.hardware_id,
        token=token or issued,
        capabilities=frozenset(record.capabilities if capabilities is None else capabilities),
        display=display,
        features=frozenset(features),
    )


def quick_settings(**changes: float) -> SessionSettings:
    """Settings for tests: what a session waits for, in fractions of a second."""
    fields: dict[str, Any] = {
        "hello_timeout_s": 1.0,
        "idle_timeout_s": 5.0,
        "command_timeout_s": 1.0,
        "send_timeout_s": 1.0,
    }
    return SessionSettings(**{**fields, **changes})


@dataclass
class Connected:
    """A device that is connected: its session, its end of the wire, and what it was told."""

    session: Session
    wire: FakeWire
    ready: Ready
    task: "asyncio.Task[None]"


class Rig:
    """An edge with some devices registered, a conversation service that answers as told, and the
    means to connect devices to it."""

    def __init__(
        self,
        *devices: tuple[SystemDeviceRecord, str],
        settings: SessionSettings | None = None,
        speech: FakeSpeech | None = None,
        new_id: Callable[[str], str] | None = None,
    ) -> None:
        self.devices = devices or (make_device(),)
        self.registry = FileDeviceRegistry(TEST, list(self.devices))
        self.sessions = Sessions()
        self.conversation = FakeConversation()
        self.speech = speech
        self.settings = settings or quick_settings()
        self._new_id = new_id
        self._tasks: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> "Rig":
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def open(self) -> tuple[Session, FakeWire, "asyncio.Task[None]"]:
        """A connection that has been made and has said nothing yet."""
        wire = FakeWire()
        extra: dict[str, Any] = {} if self._new_id is None else {"new_id": self._new_id}
        session = Session(
            wire,
            registry=self.registry,
            directory=self.sessions,
            conversation=self.conversation,
            speech=self.speech,
            settings=self.settings,
            **extra,
        )
        task = asyncio.create_task(session.run())
        self._tasks.append(task)
        return session, wire, task

    async def connect(
        self, device: tuple[SystemDeviceRecord, str] | None = None, **said: Any
    ) -> Connected:
        """A device that says hello and is told it is ready."""
        session, wire, task = self.open()
        wire.say(hello(device or self.devices[0], **said))
        [ready] = await wire.wait_for("ready")
        return Connected(session, wire, ready, task)


class Serving:
    """An edge that is listening on a real socket, and the addresses to reach it at."""

    def __init__(self, rig: Rig, server: uvicorn.Server, port: int) -> None:
        self.rig = rig
        self.server = server
        self.port = port

    @property
    def websocket_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/connect"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class ReportingServer(uvicorn.Server):
    """A server that says when it is listening."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.listening = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        self.listening.set()


async def forgotten(rig: Rig) -> bool:
    """Whether the edge has forgotten every device, waiting a little for it to notice."""
    for _ in range(200):
        if len(rig.sessions) == 0:
            return True
        await asyncio.sleep(0.01)
    return False


@asynccontextmanager
async def serving(rig: Rig) -> AsyncIterator[Serving]:
    """The rig's edge, served by uvicorn on a port of the loopback that the system chooses. The
    server is what real devices meet: the WebSocket implementation and the limit of a frame are
    the ones of ``python -m factotum.edge``."""
    app = create_app(
        DevAuthenticator(TEST, tenant_id=TENANT, user_id=USER),
        sessions=rig.sessions,
        registry=rig.registry,
        conversation=rig.conversation,
        speech=rig.speech,
        session_settings=rig.settings,
    )
    server = ReportingServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            ws="websockets-sansio",
            ws_max_size=MAX_FRAME_BYTES,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(server.listening.wait(), 5)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield Serving(rig, server, port)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
