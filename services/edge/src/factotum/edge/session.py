"""One device's connection, from ``hello`` to goodbye.

A session is what the edge holds while a device is connected. It authenticates the device, tells it
what it may do, turns each utterance into a turn of the conversation service and streams the answer
back as parts its capabilities can render, sends the device commands and waits for their
acknowledgement, and notices when the device has gone.

Things it holds to, because a device is a small computer on a home network and may do anything:

* Nothing is believed before ``hello``, and a device that does not say who it is soon enough is
  closed. A device that fails to authenticate is told only that it did, never which part was wrong.
* One answer at a time. Another utterance interrupts the one being answered, whose turn is stopped
  (and with it the model that is writing it), and the device is told that it was.
* A device that says nothing for too long, or cannot be written to, is gone. Its turn is stopped,
  its commands are answered as offline, and it is forgotten by the edge.
* What goes wrong inside is the device's to be told in a code, never in a stack trace.
"""

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from factotum.contracts.audio import AudioPayload
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.commands import CommandResult, CommandStatus, DeviceCommand
from factotum.contracts.conversation import Failed
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.edge import (
    PROTOCOL_VERSION,
    Ack,
    AudioUtterance,
    Bye,
    Cancel,
    CloseCode,
    CommandFrame,
    ErrorFrame,
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
    principal acts as. ``None`` where a session is given none: an ``audio_utterance`` is then
    refused, and a ``Speech`` part goes out as text alone."""

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
    ) -> None:
        self.hello_timeout_s = hello_timeout_s
        self.heartbeat_s = heartbeat_s
        self.idle_timeout_s = idle_timeout_s
        self.command_timeout_s = command_timeout_s
        self.send_timeout_s = send_timeout_s

    @classmethod
    def of(cls, settings: EdgeSettings) -> "SessionSettings":
        return cls(
            hello_timeout_s=settings.edge_hello_timeout_s,
            heartbeat_s=settings.edge_heartbeat_s,
            idle_timeout_s=settings.edge_idle_timeout_s,
            command_timeout_s=settings.edge_command_timeout_s,
            send_timeout_s=settings.edge_send_timeout_s,
        )


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
            try:
                async with asyncio.timeout(self._settings.idle_timeout_s):
                    text = await self._wire.receive()
            except TimeoutError:
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

    async def _cleanup(self) -> None:
        self._closed = True
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

    async def _on_utterance(self, utterance: Utterance) -> None:
        if Capability.AUDIO_IN not in self.capabilities:
            await self._send(
                ErrorFrame(
                    code="capability_missing",
                    message="the device did not declare a microphone, so it cannot start a "
                    "conversation",
                    ref=utterance.id,
                )
            )
            return
        await self._stop_turn()  # the person spoke over the answer to the last one
        task = asyncio.create_task(
            self._answer(utterance.id, utterance.text), name=f"turn-{utterance.id}"
        )
        self._turn = (utterance.id, task)

    async def _on_audio_utterance(self, utterance: AudioUtterance) -> None:
        if Capability.AUDIO_IN not in self.capabilities:
            await self._send(
                ErrorFrame(
                    code="capability_missing",
                    message="the device did not declare a microphone, so it cannot start a "
                    "conversation",
                    ref=utterance.id,
                )
            )
            return
        if self._speech is None:
            await self._send(
                ErrorFrame(
                    code="speech_not_configured",
                    message="the edge cannot turn a recording into words",
                    ref=utterance.id,
                )
            )
            return
        await self._stop_turn()  # the person spoke over the answer to the last one
        task = asyncio.create_task(self._answer_audio(utterance), name=f"turn-{utterance.id}")
        self._turn = (utterance.id, task)

    async def _on_cancel(self, cancel: Cancel) -> None:
        if self._turn is not None and self._turn[0] == cancel.id:
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

    async def _answer_audio(self, utterance: AudioUtterance) -> None:
        assert self.principal is not None  # noqa: S101 - a session that answers has said hello
        assert self._speech is not None  # noqa: S101 - checked by _on_audio_utterance
        try:
            text = await self._speech.transcribe(
                Owner.of_subject(self.principal.acts_as), utterance.audio
            )
        except asyncio.CancelledError:
            raise
        except FactotumError as error:
            await self._end_unheard(utterance.id, error.code, error.message)
            return
        except Exception:
            logger.exception(
                "a recording could not be transcribed", extra={"session": self.session_id}
            )
            await self._end_unheard(utterance.id, "internal_error", "I could not hear that.")
            return
        if not text.strip():
            await self._end_unheard(utterance.id, "nothing_heard", "I didn't catch that.")
            return
        await self._answer(utterance.id, text)

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

    async def _send(
        self, frame: PartFrame | TurnEnded | CommandFrame | Pong | Ready | ErrorFrame
    ) -> None:
        if self._closed:
            raise Disconnected
        async with self._send_lock:
            try:
                async with asyncio.timeout(self._settings.send_timeout_s):
                    await self._wire.send(encode_frame(frame))
            except TimeoutError:
                raise Disconnected from None
