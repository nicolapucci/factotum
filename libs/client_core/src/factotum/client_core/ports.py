"""What a client is plugged into: who it is, what it shows, and what it does when it is told to.

A client is built from the ports that its device has. A speaker with a microphone has an
identity, a way to say what it was told (a ``Renderer`` that does nothing at all with text and
everything with sound), and hands for the commands it can run (a volume, a light); a screen has a
renderer that draws; a phone has all of it. The client declares to the edge what the device can do,
and what needs more than that is never sent to it (ADR 0013): when it is anyway, the client says
so, loudly.
"""

from dataclasses import dataclass, field
from typing import Protocol

from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.cards import Card
from factotum.contracts.commands import CommandResult, CommandStatus, DeviceCommand
from factotum.contracts.edge import ErrorFrame, TurnEnded
from factotum.contracts.response import StatusState


class Identity(Protocol):
    """Who a device is, as it says it when it connects."""

    @property
    def hardware_id(self) -> str: ...

    @property
    def token(self) -> str:
        """The credential it was given when it was paired."""
        ...

    @property
    def capabilities(self) -> frozenset[Capability]: ...

    @property
    def display(self) -> DisplayInfo | None: ...


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """An identity, for a client that does not have a profile to take it from."""

    hardware_id: str
    token: str = field(repr=False)
    capabilities: frozenset[Capability]
    display: DisplayInfo | None = None


class Renderer(Protocol):
    """What a device shows, or does with sound, of what happens: the parts of an answer one by one,
    what the edge took a recording for, and what is wrong."""

    def speech(self, text: str) -> None: ...

    def text(self, text: str) -> None: ...

    def card(self, card: Card) -> None: ...

    def media(self, title: str | None, url: str) -> None: ...

    def status(self, state: StatusState) -> None: ...

    def event(self, what: str) -> None:
        """A command that was run on the device: the volume, a light, the music."""
        ...

    def heard(self, text: str) -> None:
        """What the edge took a recording for, before it answers it."""
        ...

    def turn_ended(self, ended: TurnEnded) -> None: ...

    def error(self, error: ErrorFrame) -> None:
        """The edge said something is wrong."""
        ...

    def violation(self, what: str) -> None:
        """The edge did something the protocol does not allow: a bug, and worth saying loudly."""
        ...

    def notice(self, what: str) -> None:
        """Something about the client itself: connected, disconnected."""
        ...


class CommandHandler(Protocol):
    """What runs a command the edge sends, and says what became of it."""

    async def execute(self, command: DeviceCommand) -> CommandResult: ...


class CommandDriver(CommandHandler, Protocol):
    """The hands of one part of a device (its volume, its light, its screen): what it runs, and the
    capabilities it stands for."""

    @property
    def capabilities(self) -> frozenset[Capability]: ...


class CommandRouter:
    """The hands of a device, put together: each command goes to the driver that has the capability
    it needs, and a command that none has is ``unsupported`` (which is what the edge is told, and is
    the edge's to have known: it only sends what a device declared)."""

    def __init__(self, *drivers: CommandDriver) -> None:
        self._drivers = drivers

    @property
    def capabilities(self) -> frozenset[Capability]:
        """What the drivers can do, all of them: what a device with these hands declares."""
        return frozenset().union(*(driver.capabilities for driver in self._drivers))

    async def execute(self, command: DeviceCommand) -> CommandResult:
        for driver in self._drivers:
            if command.requires in driver.capabilities:
                return await driver.execute(command)
        return CommandResult(
            status=CommandStatus.UNSUPPORTED,
            detail=f"this device has no {command.requires.value!r}",
        )
