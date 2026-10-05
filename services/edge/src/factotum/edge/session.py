"""One device's connection, from ``hello`` to goodbye.

A session is what the edge holds while a device is connected. It authenticates the device, tells it
what it may do, turns each utterance into a turn of the conversation service and streams the answer
back as parts its capabilities can render, sends the device commands and waits for their
acknowledgement, and notices when the device has gone.

Things it holds to, because a device is a small computer on a home network and may do anything:

* Nothing is believed before ``hello``, and a device that does not say who it is soon enough is
  closed. A device that fails to authenticate is told only that it did, never which part was wrong.
* One answer at a time. Another utterance interrupts the one being answered, whose turn is stopped
  (and with it the model that is writing it), and the device is told that it was. A person who
  starts to talk (``audio_start``) interrupts it at once, not when they have finished.
* A stream of audio is bounded: by its size, by how long the device may be quiet in the middle of
  it, and by the order of its chunks. Whatever ends one without an answer says how, once
  (ADR 0022).
* A device that says nothing for too long, or cannot be written to, is gone. Its turn is stopped,
  its commands are answered as offline, and it is forgotten by the edge.
* What goes wrong inside is the device's to be told in a code, never in a stack trace.
"""

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from factotum.contracts.audio import MAX_CHUNK_BYTES, MAX_STREAM_BYTES, AudioPayload, wav_of
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.commands import CommandResult, CommandStatus, DeviceCommand
from factotum.contracts.conversation import MAX_MESSAGE_CHARS, Failed
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.edge import (
    PROTOCOL_VERSION,
    Ack,
    AudioChunk,
    AudioEnd,
    AudioStart,
    AudioUtterance,
    Bye,
    Cancel,
    CloseCode,
    CommandFrame,
    ErrorFrame,
    FromEdge,
    Heard,
    Hello,
    PartFrame,
    Ping,
    Pong,
    Ready,
    TurnEnded,
    TurnStatus,
    Utterance,
    decode_device_frame,
    encode_frame,
)
from factotum.contracts.errors import FactotumError, InvalidFrameError
from factotum.contracts.identity import Owner, Principal, Role
from factotum.contracts.ids import DeviceId, TenantId
from factotum.contracts.response import ResponsePart, Speech
from factotum.edge.conversation import TurnSource
from factotum.edge.parts import TurnParts
from factotum.edge.registry import DeviceRegistry, principal_of
from factotum.edge.settings import EdgeSettings

logger = logging.getLogger("factotum.edge")


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


class SessionDirectory(Protocol):
    """Where the sessions that are open are kept, so that a command can find its device."""

    async def attach(self, session: "Session") -> None: ...

    def detach(self, session: "Session") -> None: ...


class SpeechSource(Protocol):
    """What a session needs to turn a recording into words, and words into one, for the owner the
    principal acts as. ``None`` where a session is given none: a recording (an ``audio_utterance``,
    or a stream of audio) is then refused, and a ``Speech`` part goes out as text alone."""

    async def transcribe(self, owner: Owner, audio: AudioPayload) -> str: ...

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload: ...


class SessionSettings:
    """What a session waits for, and for how long."""

    def __init__(
        self,
        *,
        hello_timeout_s: float = 10.0,
        heartbeat_s: int = 20,
        idle_timeout_s: float = 60.0,
        command_timeout_s: float = 10.0,
        send_timeout_s: float = 10.0,
        stream_stall_s: float = 5.0,
    ) -> None:
        self.hello_timeout_s = hello_timeout_s
        self.heartbeat_s = heartbeat_s
        self.idle_timeout_s = idle_timeout_s
        self.command_timeout_s = command_timeout_s
        self.send_timeout_s = send_timeout_s
        self.stream_stall_s = stream_stall_s

    @classmethod
    def of(cls, settings: EdgeSettings) -> "SessionSettings":
        return cls(
            hello_timeout_s=settings.edge_hello_timeout_s,
            heartbeat_s=settings.edge_heartbeat_s,
            idle_timeout_s=settings.edge_idle_timeout_s,
            command_timeout_s=settings.edge_command_timeout_s,
            send_timeout_s=settings.edge_send_timeout_s,
            stream_stall_s=settings.edge_stream_stall_s,
        )


@dataclass
class _Stream:
    """A person talking to the device, as far as the edge has heard: the samples so far, and when
    the next chunk is due."""

    id: str
    sample_rate: int
    due: float
    """When the stream is stalled if no chunk has come, by the clock of the event loop."""

    samples: bytearray = field(default_factory=bytearray)
    next_seq: int = 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


class Session:
    def __init__(
        self,
        wire: Wire,
        *,
        registry: DeviceRegistry,
        directory: SessionDirectory,
        conversation: TurnSource,
        speech: SpeechSource | None = None,
        settings: SessionSettings | None = None,
        now: Callable[[], datetime] = _utc_now,
        new_id: Callable[[str], str] = _new_id,
    ) -> None:
        self._wire = wire
        self._registry = registry
        self._directory = directory
        self._conversation = conversation
        self._speech = speech
        self._settings = settings or SessionSettings()
        self._new_id = new_id
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[CommandResult]] = {}
        self._turn: tuple[str, asyncio.Task[None]] | None = None
        self._stream: _Stream | None = None
        self._closed = False
        self.session_id = new_id("s")
        self.connected_at = now()
        self.record: SystemDeviceRecord | None = None
        self.principal: Principal | None = None
        self.capabilities: frozenset[Capability] = frozenset()
        self.display: DisplayInfo | None = None

    # ---- what the rest of the edge asks of a session ----

    @property
    def key(self) -> tuple[TenantId, DeviceId]:
        """Whose session it is: only known once the device has said hello."""
        if self.record is None:
            raise RuntimeError("the device has not said who it is yet")
        return self.record.tenant_id, self.record.id

    async def command(
        self, command: DeviceCommand, timeout_s: float | None = None
    ) -> CommandResult:
        """Send the device a command and wait for what became of it."""
        if command.requires not in self.capabilities:
            return CommandResult(
                status=CommandStatus.UNSUPPORTED,
                detail=f"the device did not declare {command.requires.value!r}",
            )
        if self._closed:
            return CommandResult(status=CommandStatus.OFFLINE, detail="the device is not connected")
        identity = self._new_id("c")
        answered: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        self._pending[identity] = answered
        try:
            await self._send(CommandFrame(id=identity, command=command))
            async with asyncio.timeout(timeout_s or self._settings.command_timeout_s):
                return await answered
        except TimeoutError:
            return CommandResult(
                status=CommandStatus.TIMEOUT, detail="the device did not answer in time"
            )
        except Disconnected:
            return CommandResult(status=CommandStatus.OFFLINE, detail="the device is not connected")
        finally:
            self._pending.pop(identity, None)

    async def close(self, code: CloseCode, reason: str = "") -> None:
        """End the connection. The loop that reads from it notices, and cleans up."""
        self._closed = True
        with contextlib.suppress(Exception):
            await self._wire.close(int(code), reason)

    # ---- the life of a connection ----

    async def run(self) -> None:
        try:
            if await self._greet():
                await self._serve()
        except Disconnected:
            pass
        except Exception:
            # A bug is the edge's to fix and the device's to be spared.
            logger.exception("a session failed", extra={"session": self.session_id})
            await self.close(CloseCode.INTERNAL_ERROR, "the edge failed")
        finally:
            await self._cleanup()

    async def _greet(self) -> bool:
        """The device's first frame, and what it makes of it. False when the device is refused."""
        try:
            async with asyncio.timeout(self._settings.hello_timeout_s):
                text = await self._wire.receive()
        except TimeoutError:
            await self._refuse("hello_required", "say hello first", CloseCode.POLICY)
            return False
        try:
            hello = decode_device_frame(text)
        except InvalidFrameError as error:
            await self._refuse(error.code, error.message, CloseCode.PROTOCOL_ERROR)
            return False
        if not isinstance(hello, Hello):
            await self._refuse("hello_required", "say hello first", CloseCode.POLICY)
            return False
        if hello.protocol_version != PROTOCOL_VERSION:
            await self._refuse(
                "unsupported_protocol",
                f"the edge speaks version {PROTOCOL_VERSION} of the protocol",
                CloseCode.UNSUPPORTED_VERSION,
            )
            return False

        record = await self._registry.authenticate(hello.hardware_id, hello.token)
        if record is None:
            await self._refuse(
                "auth_failed",
                "the device is not known, or its credential is wrong",
                CloseCode.POLICY,
                claimed=hello.hardware_id,
            )
            return False

        allowed = hello.capabilities & record.capabilities
        self.record = record
        self.principal = principal_of(record, user_roles=frozenset({Role.MEMBER}))
        self.capabilities = allowed
        self.display = (hello.display or record.display) if Capability.DISPLAY in allowed else None
        await self._directory.attach(self)
        await self._send(
            Ready(
                protocol_version=PROTOCOL_VERSION,
                session_id=self.session_id,
                device_id=record.id,
                name=record.name,
                capabilities=allowed,
                unregistered=hello.capabilities - record.capabilities,
                display=self.display,
                heartbeat_s=self._settings.heartbeat_s,
            )
        )
        logger.info(
            "a device connected",
            extra={
                "device": str(record.id),
                "device_name": record.name,
                "session": self.session_id,
                "capabilities": sorted(c.value for c in allowed),
            },
        )
        return True

    async def _serve(self) -> None:
        while True:
            wait_s, for_a_chunk = self._how_long_to_wait()
            try:
                async with asyncio.timeout(wait_s):
                    text = await self._wire.receive()
            except TimeoutError:
                if for_a_chunk:
                    # A device that died in the middle of a sentence is not a device that is idle:
                    # what it began is over, and it is still told, and still there.
                    await self._drop_stream("stream_stalled", "no chunk of the stream came in time")
                    continue
                await self._refuse("idle", "the device said nothing for too long", CloseCode.IDLE)
                return
            try:
                frame = decode_device_frame(text)
            except InvalidFrameError as error:
                await self._refuse(error.code, error.message, CloseCode.PROTOCOL_ERROR)
                return
            match frame:
                case Ping():
                    await self._send(Pong())
                case Utterance():
                    await self._on_utterance(frame)
                case AudioUtterance():
                    await self._on_audio_utterance(frame)
                case AudioStart():
                    await self._on_audio_start(frame)
                case AudioChunk():
                    await self._on_audio_chunk(frame)
                case AudioEnd():
                    await self._on_audio_end(frame)
                case Cancel():
                    await self._on_cancel(frame)
                case Ack():
                    self._on_ack(frame)
                case Bye():
                    return
                case Hello():
                    await self._refuse(
                        "invalid_frame",
                        "the device has already said hello",
                        CloseCode.PROTOCOL_ERROR,
                    )
                    return

    def _how_long_to_wait(self) -> tuple[float, bool]:
        """How long to wait for the next frame, and whether it is for a chunk: as long as a device
        may say nothing at all, or less when a stream is open and its next chunk is due before."""
        idle_s = self._settings.idle_timeout_s
        if self._stream is None:
            return idle_s, False
        due_s = max(0.0, self._stream.due - asyncio.get_running_loop().time())
        return (due_s, True) if due_s < idle_s else (idle_s, False)

    async def _cleanup(self) -> None:
        self._closed = True
        self._stream = None  # nobody is left to be told what became of it
        await self._stop_turn()
        for answered in self._pending.values():
            if not answered.done():
                answered.set_result(
                    CommandResult(status=CommandStatus.OFFLINE, detail="the device disconnected")
                )
        if self.record is not None:
            self._directory.detach(self)
            logger.info("a device disconnected", extra={"session": self.session_id})
        with contextlib.suppress(Exception):
            await self._wire.close(int(CloseCode.NORMAL))

    async def _refuse(
        self, code: str, message: str, close: CloseCode, *, claimed: str | None = None
    ) -> None:
        """Tell the device what is wrong, and end the connection. It is logged, with the hardware id
        that was claimed when it is not known (never the credential): somebody who installs a device
        has nowhere else to find out why it does not connect."""
        extra: dict[str, str] = {"session": self.session_id, "code": code}
        if claimed is not None:
            extra["claimed_hardware_id"] = claimed
        logger.warning(
            "a device was sent away" if self.record is not None else "a device was refused",
            extra=extra,
        )
        with contextlib.suppress(Disconnected):
            await self._send(ErrorFrame(code=code, message=message, fatal=True))
        await self.close(close, code)

    # ---- what a device says ----

    async def _can_converse(self, identity: str) -> bool:
        """Whether the device may start a conversation: it is told, and nothing else happens, when
        it may not."""
        if Capability.AUDIO_IN in self.capabilities:
            return True
        await self._send(
            ErrorFrame(
                code="capability_missing",
                message="the device did not declare a microphone, so it cannot start a "
                "conversation",
                ref=identity,
            )
        )
        return False

    async def _can_hear(self, identity: str) -> bool:
        """Whether a recording from the device can be turned into words, said in the same way."""
        if not await self._can_converse(identity):
            return False
        if self._speech is None:
            await self._send(
                ErrorFrame(
                    code="speech_not_configured",
                    message="the edge cannot turn a recording into words",
                    ref=identity,
                )
            )
            return False
        return True

    async def _supersede(self) -> None:
        """The person said something new: what they were saying and what was being answered to the
        one before are given up on, and each is told that it was."""
        await self._end_stream(TurnStatus.CANCELLED)
        await self._stop_turn()

    def _begin_turn(self, identity: str, turn: Coroutine[Any, Any, None]) -> None:
        self._turn = (identity, asyncio.create_task(turn, name=f"turn-{identity}"))

    async def _on_utterance(self, utterance: Utterance) -> None:
        if not await self._can_converse(utterance.id):
            return
        await self._supersede()  # the person spoke over the answer to the last one
        self._begin_turn(utterance.id, self._answer(utterance.id, utterance.text))

    async def _on_audio_utterance(self, utterance: AudioUtterance) -> None:
        if not await self._can_hear(utterance.id):
            return
        await self._supersede()  # the person spoke over the answer to the last one
        self._begin_turn(utterance.id, self._answer_audio(utterance.id, utterance.audio))

    async def _on_audio_start(self, start: AudioStart) -> None:
        if not await self._can_hear(start.id):
            return
        # The person spoke over the answer to the last one as soon as they began, not when they
        # have finished: it stops now.
        await self._supersede()
        self._stream = _Stream(start.id, start.sample_rate, due=self._due())

    async def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        stream = self._stream
        if stream is None or stream.id != chunk.id:
            await self._unknown_stream(chunk.id)
        elif len(chunk.data) > MAX_CHUNK_BYTES:
            await self._drop_stream(
                "chunk_too_big", f"a chunk of a stream is at most {MAX_CHUNK_BYTES} bytes"
            )
        elif chunk.seq != stream.next_seq:
            await self._drop_stream(
                "stream_out_of_order",
                f"chunk {stream.next_seq} was expected and chunk {chunk.seq} came",
            )
        elif len(stream.samples) + len(chunk.data) > MAX_STREAM_BYTES:
            await self._drop_stream(
                "audio_too_long", f"a stream is at most {MAX_STREAM_BYTES} bytes of audio"
            )
        else:
            stream.samples.extend(chunk.data)
            stream.next_seq += 1
            stream.due = self._due()

    async def _on_audio_end(self, end: AudioEnd) -> None:
        stream = self._stream
        if stream is None or stream.id != end.id:
            await self._unknown_stream(end.id)
            return
        self._stream = None
        if len(stream.samples) < 2:  # not one whole sample: there is nothing to listen to
            await self._end_unheard(stream.id, "nothing_heard", "I didn't catch that.")
            return
        recording = wav_of(bytes(stream.samples), stream.sample_rate)
        self._begin_turn(stream.id, self._answer_audio(stream.id, recording))

    async def _unknown_stream(self, identity: str) -> None:
        """A frame of a stream that is not the one that is open (it was dropped, or it never was):
        said, and nothing else happens, so that a late frame of one does not end the one that is."""
        await self._send(
            ErrorFrame(
                code="unknown_stream",
                message="no stream of that id is open",
                ref=identity,
            )
        )

    def _due(self) -> float:
        """When a stream that has just heard from the device is stalled, if it does not again."""
        return asyncio.get_running_loop().time() + self._settings.stream_stall_s

    async def _end_stream(
        self, status: TurnStatus, code: str | None = None, message: str | None = None
    ) -> None:
        """The stream that is open ends without being answered: say how, once."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        with contextlib.suppress(Disconnected):
            await self._send(
                TurnEnded(utterance_id=stream.id, status=status, code=code, message=message)
            )

    async def _drop_stream(self, code: str, message: str) -> None:
        """The stream that is open is the device's mistake or its misfortune: it is over, failed."""
        logger.warning("a stream was dropped", extra={"session": self.session_id, "code": code})
        await self._end_stream(TurnStatus.FAILED, code, message)

    async def _on_cancel(self, cancel: Cancel) -> None:
        if self._stream is not None and self._stream.id == cancel.id:
            await self._end_stream(TurnStatus.CANCELLED)
        elif self._turn is not None and self._turn[0] == cancel.id:
            await self._stop_turn()

    def _on_ack(self, ack: Ack) -> None:
        answered = self._pending.get(ack.id)
        if answered is not None and not answered.done():
            answered.set_result(ack.result)

    async def _stop_turn(self) -> None:
        """Stop the answer being given, if there is one, and wait until it is over."""
        if self._turn is None:
            return
        _, task = self._turn
        self._turn = None
        if not task.done():
            task.cancel()
        await asyncio.wait({task})

    # ---- what the edge says ----

    async def _answer_audio(self, utterance_id: str, recording: AudioPayload) -> None:
        """A recording, however it came (whole, or put together from a stream), is turned into
        words and answered as the words would be had they been typed."""
        try:
            text = await self._hear(utterance_id, recording)
        except asyncio.CancelledError:
            # The person spoke again while the recording was still being turned into words.
            with contextlib.suppress(Disconnected):
                await self._send(TurnEnded(utterance_id=utterance_id, status=TurnStatus.CANCELLED))
            raise
        if text is not None:
            await self._answer(utterance_id, text)

    async def _hear(self, utterance_id: str, recording: AudioPayload) -> str | None:
        """What a recording says, told to the device before the answer to it begins. None when the
        turn is over already: nothing could be made of it, or the device is gone."""
        assert self.principal is not None  # noqa: S101 - a session that answers has said hello
        assert self._speech is not None  # noqa: S101 - checked before a recording is accepted
        try:
            text = (
                await self._speech.transcribe(Owner.of_subject(self.principal.acts_as), recording)
            ).strip()
        except FactotumError as error:
            await self._end_unheard(utterance_id, error.code, error.message)
            return None
        except Exception:
            logger.exception(
                "a recording could not be transcribed", extra={"session": self.session_id}
            )
            await self._end_unheard(utterance_id, "internal_error", "I could not hear that.")
            return None
        if not text:
            await self._end_unheard(utterance_id, "nothing_heard", "I didn't catch that.")
            return None
        try:
            await self._send(Heard(utterance_id=utterance_id, text=text[:MAX_MESSAGE_CHARS]))
        except Disconnected:
            return None
        return text

    async def _end_unheard(self, utterance_id: str, code: str, message: str) -> None:
        """A recording that was never turned into words ends its turn here and now: there is no
        text to run through the conversation, and nothing was said yet to take back."""
        with contextlib.suppress(Disconnected):
            await self._send(
                TurnEnded(
                    utterance_id=utterance_id, status=TurnStatus.FAILED, code=code, message=message
                )
            )

    async def _spoken(self, part: Speech) -> Speech:
        """``part`` with ``audio`` filled in, when a text-to-speech model is bound; unchanged
        otherwise, so a device without one still gets the words to show or to read out itself."""
        if self._speech is None:
            return part
        assert self.principal is not None  # noqa: S101 - a session that answers has said hello
        try:
            audio = await self._speech.synthesize(
                Owner.of_subject(self.principal.acts_as), part.text
            )
        except FactotumError:
            return part
        except Exception:
            logger.exception(
                "a sentence could not be synthesized", extra={"session": self.session_id}
            )
            return part
        return part.model_copy(update={"audio": audio})

    async def _answer(self, utterance_id: str, text: str) -> None:
        assert self.principal is not None  # noqa: S101 - a session that answers has said hello
        parts = TurnParts(self.capabilities)
        seq = 0
        status, code, message = TurnStatus.FINISHED, None, None

        async def deliver(items: list[ResponsePart]) -> None:
            nonlocal seq
            for part in items:
                if isinstance(part, Speech):
                    part = await self._spoken(part)
                await self._send(PartFrame(utterance_id=utterance_id, seq=seq, part=part))
                seq += 1

        try:
            events = self._conversation.turn(self.principal, text)
            async with contextlib.aclosing(events):
                async for event in events:
                    await deliver(parts.feed(event))
                    if isinstance(event, Failed):
                        status, code, message = TurnStatus.FAILED, event.code, event.message
            await deliver(parts.finish())
        except asyncio.CancelledError:
            with contextlib.suppress(Disconnected):
                await self._send(TurnEnded(utterance_id=utterance_id, status=TurnStatus.CANCELLED))
            raise
        except Disconnected:
            return
        except FactotumError as error:
            status, code, message = TurnStatus.FAILED, error.code, error.message
            try:
                await deliver(parts.failure(error.code))
            except Disconnected:
                return
        except Exception:
            logger.exception("an answer failed", extra={"session": self.session_id})
            status, code, message = TurnStatus.FAILED, "internal_error", "the answer failed"
            try:
                await deliver(parts.failure(code))
            except Disconnected:
                return
        with contextlib.suppress(Disconnected):
            await self._send(
                TurnEnded(utterance_id=utterance_id, status=status, code=code, message=message)
            )

    async def _send(self, frame: FromEdge) -> None:
        if self._closed:
            raise Disconnected
        async with self._send_lock:
            try:
                async with asyncio.timeout(self._settings.send_timeout_s):
                    await self._wire.send(encode_frame(frame))
            except TimeoutError:
                raise Disconnected from None
