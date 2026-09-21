"""The protocol between a system device and the edge, version 1.

A device opens a WebSocket to the edge and speaks in JSON text frames, each an object with a
``type``. This module is the whole of it; ``docs/edge.md`` says why it is the way it is.

The device speaks first. ``hello`` says which protocol it speaks, who it is (a hardware id and the
credential it was given when it was paired) and what it can do; the edge answers ``ready``, or an
``error`` and closes.

* A device that can start conversations (``audio_in``) sends an ``utterance``: what was said, as
  text. The edge answers with ``part`` frames, the pieces of the answer in the order they are to be
  rendered, each of a kind the device declared it can render, and then ``turn_ended``. Another
  ``utterance`` interrupts the answer to the one before (the person spoke over it), and so does a
  ``cancel``.
* When something wants the device to do something (set the volume, show a card) the edge sends a
  ``command``, and the device answers with an ``ack`` that carries the result.
* ``ping`` and ``pong`` say that each side is still there. A device that says nothing for too long
  is taken to be gone.

A frame is never trusted to be what it says: it is parsed into one of the models below, and one that
is not is refused (``InvalidFrameError``) without being echoed, since it may carry a credential.
"""

from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    TypeAdapter,
    ValidationError,
    field_serializer,
    model_validator,
)

from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.commands import CommandResult, DeviceCommand
from factotum.contracts.conversation import MAX_MESSAGE_CHARS
from factotum.contracts.errors import InvalidFrameError
from factotum.contracts.ids import DeviceId
from factotum.contracts.response import ResponsePart

PROTOCOL_VERSION = 1

MAX_FRAME_BYTES = 64 * 1024
"""The largest frame either side sends or accepts. Devices send small frames, and the parts of an
answer are cut to fit long before this."""

HEARTBEAT_SECONDS = 20
"""How often a device that has nothing else to say is asked to say that it is there."""

FRAME_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
HARDWARE_ID_PATTERN = r"^[A-Za-z0-9:._-]{1,128}$"

FrameId = Annotated[str, Field(pattern=FRAME_ID_PATTERN)]
"""What an utterance, a command or a session is called, so that what answers it can say which."""


class CloseCode(IntEnum):
    """Why the edge closes a connection (the application range of WebSocket close codes)."""

    NORMAL = 1000
    GOING_AWAY = 1001
    PROTOCOL_ERROR = 1002
    POLICY = 1008
    TOO_BIG = 1009
    INTERNAL_ERROR = 1011
    REPLACED = 4001
    """The same device connected again: the last connection wins, since the old one may be a dead
    one that has not been noticed yet."""

    IDLE = 4002
    UNSUPPORTED_VERSION = 4003


def _sorted(capabilities: frozenset[Capability]) -> list[str]:
    return sorted(capability.value for capability in capabilities)


# ---- from the device to the edge ----------------------------------------------------------------


class ClientInfo(Contract):
    """What software is on the device, for whoever reads the logs."""

    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)


class Hello(Contract):
    """The first frame: who the device is, and what it can do."""

    type: Literal["hello"] = "hello"
    protocol_version: int = Field(ge=1)
    hardware_id: str = Field(pattern=HARDWARE_ID_PATTERN)
    token: str = Field(min_length=1, max_length=512, repr=False)
    """The credential issued when the device was paired. It never appears in a repr or a log."""

    capabilities: frozenset[Capability]
    display: DisplayInfo | None = None
    client: ClientInfo | None = None

    @field_serializer("capabilities")
    def _capabilities_in_order(self, capabilities: frozenset[Capability]) -> list[str]:
        return _sorted(capabilities)

    @model_validator(mode="after")
    def _a_display_needs_the_capability(self) -> Self:
        if self.display is not None and Capability.DISPLAY not in self.capabilities:
            raise ValueError("display information requires the 'display' capability")
        return self


class Utterance(Contract):
    """Something a person said to the device, as text."""

    type: Literal["utterance"] = "utterance"
    id: FrameId
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class Cancel(Contract):
    """Stop answering an utterance (a button, or the person saying stop)."""

    type: Literal["cancel"] = "cancel"
    id: FrameId


class Ack(Contract):
    """What became of a command."""

    type: Literal["ack"] = "ack"
    id: FrameId
    result: CommandResult


class Ping(Contract):
    type: Literal["ping"] = "ping"


class Bye(Contract):
    """The device is going away on purpose."""

    type: Literal["bye"] = "bye"


DeviceFrame = Annotated[Hello | Utterance | Cancel | Ack | Ping | Bye, Field(discriminator="type")]


# ---- from the edge to the device ----------------------------------------------------------------


class Ready(Contract):
    """The device is known: this is what it is, as far as the edge is concerned."""

    type: Literal["ready"] = "ready"
    protocol_version: int = Field(ge=1)
    session_id: FrameId
    device_id: DeviceId
    name: str = Field(min_length=1, max_length=100)
    capabilities: frozenset[Capability]
    """What the device may do: what it declared, of what it is registered for."""

    unregistered: frozenset[Capability] = frozenset()
    """What it declared without being registered for it: ignored, and said, so that whoever
    installs a device finds out."""

    display: DisplayInfo | None = None
    heartbeat_s: int = Field(default=HEARTBEAT_SECONDS, ge=1)

    @field_serializer("capabilities", "unregistered")
    def _capabilities_in_order(self, capabilities: frozenset[Capability]) -> list[str]:
        return _sorted(capabilities)


class PartFrame(Contract):
    """A piece of the answer to an utterance.

    Parts arrive in order (``seq`` counts them from 0) and are rendered one after the other:
    speech is spoken, text is shown as a new line, a card is shown, a status is a hint.
    """

    type: Literal["part"] = "part"
    utterance_id: FrameId
    seq: int = Field(ge=0)
    part: ResponsePart


class TurnStatus(StrEnum):
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    """Stopped: by the device, or because it spoke again."""


class TurnEnded(Contract):
    """The answer to an utterance is over, and how."""

    type: Literal["turn_ended"] = "turn_ended"
    utterance_id: FrameId
    status: TurnStatus
    code: str | None = Field(default=None, max_length=64)
    """For a failure, the stable code of what went wrong."""

    message: str | None = Field(default=None, max_length=500)


class CommandFrame(Contract):
    """Something for the device to do. It answers with an ``ack`` carrying the same ``id``."""

    type: Literal["command"] = "command"
    id: FrameId
    command: DeviceCommand


class Pong(Contract):
    type: Literal["pong"] = "pong"


class ErrorFrame(Contract):
    """Something is wrong with what the device sent, or with the device."""

    type: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(max_length=500)
    fatal: bool = False
    """The edge closes the connection after it."""

    ref: FrameId | None = None
    """The utterance or command it is about, when it is about one."""


EdgeFrame = Annotated[
    Ready | PartFrame | TurnEnded | CommandFrame | Pong | ErrorFrame, Field(discriminator="type")
]

# ---- the HTTP side of the edge: who is connected, and commands for them ------------------------


class ConnectedDevice(Contract):
    """A device that is connected to the edge right now."""

    id: DeviceId
    name: str
    hardware_id: str
    capabilities: frozenset[Capability]
    display: DisplayInfo | None = None
    session_id: FrameId
    connected_at: AwareDatetime

    @field_serializer("capabilities")
    def _capabilities_in_order(self, capabilities: frozenset[Capability]) -> list[str]:
        return _sorted(capabilities)


class ConnectedDevices(Contract):
    devices: tuple[ConnectedDevice, ...] = ()


class CommandRequest(Contract):
    """Ask the edge to have a device do something. What became of it comes back as a
    ``CommandResult``: a device that is not there is a result, not an error."""

    command: DeviceCommand


_DEVICE_FRAMES: TypeAdapter[Any] = TypeAdapter(DeviceFrame)
_EDGE_FRAMES: TypeAdapter[Any] = TypeAdapter(EdgeFrame)


def encode_frame(frame: Contract) -> str:
    """A frame as the text that goes over the wire."""
    return frame.model_dump_json(exclude_none=True)


def decode_device_frame(data: str | bytes) -> Hello | Utterance | Cancel | Ack | Ping | Bye:
    """A frame a device sent, or ``InvalidFrameError``."""
    frame: Hello | Utterance | Cancel | Ack | Ping | Bye = _decode(_DEVICE_FRAMES, data)
    return frame


def decode_edge_frame(
    data: str | bytes,
) -> Ready | PartFrame | TurnEnded | CommandFrame | Pong | ErrorFrame:
    """A frame the edge sent, or ``InvalidFrameError``."""
    frame: Ready | PartFrame | TurnEnded | CommandFrame | Pong | ErrorFrame = _decode(
        _EDGE_FRAMES, data
    )
    return frame


def _decode(adapter: TypeAdapter[Any], data: str | bytes) -> Any:
    size = len(data) if isinstance(data, bytes) else len(data.encode("utf-8"))
    if size > MAX_FRAME_BYTES:
        raise InvalidFrameError(f"a frame is at most {MAX_FRAME_BYTES} bytes")
    try:
        return adapter.validate_json(data)
    except ValidationError as error:
        # Where and why, never what: the frame may be a credential in the wrong place.
        problems = [
            {"where": ".".join(str(part) for part in problem["loc"]), "why": problem["type"]}
            for problem in error.errors(
                include_input=False, include_context=False, include_url=False
            )
        ]
        raise InvalidFrameError(
            "the frame is not one of the protocol", details={"errors": problems}
        ) from None
