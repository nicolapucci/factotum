"""The device's side of the protocol.

It says hello and is told whether it is known; keeps in touch, since a device that says nothing
for a minute is taken to be gone; sends what a person said and waits for the answer to end; and
does what the edge sends: it renders the parts of an answer, and it runs the commands it is given
and says what became of each.

It also checks the edge. A part that this device could not render, parts out of order, an answer
that nobody asked for: the edge is not to do any of it, and a device that saw it would say so.
That is what a client is good for, before there is a real one.
"""

import asyncio
import contextlib

from factotum.contracts.capabilities import Capability
from factotum.contracts.commands import CommandStatus, PlayUrl
from factotum.contracts.edge import (
    PROTOCOL_VERSION,
    Ack,
    Bye,
    Cancel,
    ClientInfo,
    CommandFrame,
    ErrorFrame,
    Hello,
    PartFrame,
    Ping,
    Pong,
    Ready,
    TurnEnded,
    Utterance,
    decode_edge_frame,
    encode_frame,
)
from factotum.contracts.errors import InvalidFrameError
from factotum.contracts.response import (
    CardPart,
    CommandPart,
    MediaPart,
    Speech,
    StatusPart,
    Text,
    required_capability,
)
from factotum.terminal.connection import Connection, ConnectionLost
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import Profile
from factotum.terminal.render import Renderer

CLIENT = ClientInfo(name="factotum-terminal", version="0.0.0")


class Refused(Exception):  # noqa: N818 - it says what the edge did, not what failed
    """The edge did not accept the device, and said why (or did not)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CannotConverse(Exception):  # noqa: N818
    """This device has no microphone, so it cannot start a conversation."""


class DeviceClient:
    def __init__(
        self,
        connection: Connection,
        profile: Profile,
        renderer: Renderer,
        device: TerminalDevice,
        *,
        hardware_id: str | None = None,
        token: str | None = None,
    ) -> None:
        self._connection = connection
        self._profile = profile
        self._renderer = renderer
        self._device = device
        self._hardware_id = hardware_id or profile.hardware_id
        self._token = token or profile.token
        self._heartbeat: asyncio.Task[None] | None = None
        self._turns: dict[str, asyncio.Event] = {}
        self._ended: dict[str, TurnEnded] = {}
        self._next_part: dict[str, int] = {}
        self._counter = 0
        self.ready: Ready | None = None
        self.active: str | None = None
        """The utterance whose answer has not ended, if there is one."""

    # ---- saying hello --------------------------------------------------------------------------

    async def connect(self) -> Ready:
        """Say who this device is. Raises ``Refused`` when the edge does not accept it."""
        hello = Hello(
            protocol_version=PROTOCOL_VERSION,
            hardware_id=self._hardware_id,
            token=self._token,
            capabilities=self._profile.capabilities,
            display=self._profile.display,
            client=CLIENT,
        )
        try:
            await self._connection.send(encode_frame(hello))
            frame = decode_edge_frame(await self._connection.recv())
        except ConnectionLost as lost:
            raise Refused(
                "connection_lost", lost.reason or "the edge closed the connection"
            ) from None
        except InvalidFrameError as error:
            raise Refused(error.code, error.message) from None
        if isinstance(frame, ErrorFrame):
            raise Refused(frame.code, frame.message)
        if not isinstance(frame, Ready):
            raise Refused("protocol_error", "the edge did not say it was ready")
        self.ready = frame
        what = ", ".join(sorted(c.value for c in frame.capabilities)) or "nothing"
        self._renderer.notice(f"connected as {frame.name} ({what})")
        if frame.unregistered:
            ignored = ", ".join(sorted(c.value for c in frame.unregistered))
            self._renderer.notice(f"not registered for {ignored}: the edge will not use it")
        self._heartbeat = asyncio.create_task(self._keep_in_touch(frame.heartbeat_s))
        return frame

    async def _keep_in_touch(self, every_s: int) -> None:
        with contextlib.suppress(ConnectionLost):
            while True:
                await asyncio.sleep(every_s)
                await self._connection.send(encode_frame(Ping()))

    # ---- being spoken to -----------------------------------------------------------------------

    async def run(self) -> ConnectionLost | None:
        """Do what the edge sends, until the connection is over. Returns how it ended."""
        lost: ConnectionLost | None = None
        try:
            while True:
                text = await self._connection.recv()
                try:
                    frame = decode_edge_frame(text)
                except InvalidFrameError as error:
                    self._renderer.violation(f"the edge sent what is not a frame: {error.message}")
                    continue
                await self._handle(frame)
        except ConnectionLost as ended:
            lost = ended
        finally:
            if self._heartbeat is not None:
                self._heartbeat.cancel()
            for event in self._turns.values():
                event.set()  # nobody is left waiting for an answer that will not come
            self.active = None
        return lost

    async def _handle(self, frame: object) -> None:
        match frame:
            case PartFrame():
                await self._part(frame)
            case TurnEnded():
                self._renderer.turn_ended(frame)
                self._ended[frame.utterance_id] = frame
                self._next_part.pop(frame.utterance_id, None)
                if self.active == frame.utterance_id:
                    self.active = None
                event = self._turns.get(frame.utterance_id)
                if event is not None:
                    event.set()
            case CommandFrame():
                result = await self._device.execute(frame.command)
                with contextlib.suppress(ConnectionLost):
                    await self._connection.send(encode_frame(Ack(id=frame.id, result=result)))
            case ErrorFrame():
                self._renderer.error(frame)
            case Pong():
                pass
            case Ready():
                self._renderer.violation("the edge said ready a second time")

    async def _part(self, frame: PartFrame) -> None:
        expected = self._next_part.get(frame.utterance_id)
        if expected is None:
            self._renderer.violation(
                "the edge sent a part of an answer that nobody is waiting for "
                f"({frame.utterance_id})"
            )
            return
        if frame.seq != expected:
            self._renderer.violation(
                f"the parts of an answer came out of order: {expected} was expected, "
                f"{frame.seq} came"
            )
        self._next_part[frame.utterance_id] = frame.seq + 1

        part = frame.part
        needed = required_capability(part)
        if needed is not None and needed not in self._profile.capabilities:
            self._renderer.violation(
                f"the edge sent a {part.type} to a device that has no {needed.value}"
            )
            return
        match part:
            case Speech():
                self._renderer.speech(part.text)
            case Text():
                self._renderer.text(part.text)
            case CardPart():
                self._renderer.card(part.card)
            case MediaPart():
                await self._device.execute(PlayUrl(url=part.url, kind=part.kind, title=part.title))
            case CommandPart():
                result = await self._device.execute(part.command)
                if result.status is not CommandStatus.OK:
                    self._renderer.violation(f"a command in the answer failed: {result.detail}")
            case StatusPart():
                self._renderer.status(part.state)

    # ---- speaking ------------------------------------------------------------------------------

    async def say(self, text: str, *, force: bool = False) -> str:
        """Say something to the assistant, and return what to call it. A device with no microphone
        cannot: ``force`` says it anyway, to see what the edge makes of it."""
        if not force and Capability.AUDIO_IN not in self._profile.capabilities:
            raise CannotConverse("this device has no microphone, so it cannot start a conversation")
        self._counter += 1
        identity = f"u{self._counter}"
        self._turns[identity] = asyncio.Event()
        self._next_part[identity] = 0
        self.active = identity
        await self._connection.send(encode_frame(Utterance(id=identity, text=text)))
        return identity

    async def wait_for_turn(self, identity: str) -> TurnEnded | None:
        """The answer to an utterance is over: how, or nothing if the connection ended first."""
        event = self._turns.get(identity)
        if event is None:
            return None
        await event.wait()
        return self._ended.get(identity)

    async def cancel(self, identity: str | None = None) -> bool:
        """Stop the answer to an utterance (the one being answered, if none is named). False when
        there is nothing being answered."""
        identity = identity or self.active
        if identity is None:
            return False
        await self._connection.send(encode_frame(Cancel(id=identity)))
        return True

    async def close(self) -> None:
        """Say goodbye and go."""
        with contextlib.suppress(ConnectionLost):
            await self._connection.send(encode_frame(Bye()))
        with contextlib.suppress(ConnectionLost):
            await self._connection.close()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await asyncio.wait({self._heartbeat})
