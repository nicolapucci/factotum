"""The interface for system devices, with ready-made methods.

Each method builds one command of the shared vocabulary (``factotum.contracts.commands``), checks
that the device declared the capability the command needs, and hands it to
:meth:`SystemDevice.send`. Whoever implements ``send`` decides how the command reaches the device:
the edge does it over the device's WebSocket and waits for the acknowledgement, a test double just
records it.

Skills do not call these methods (they run in a sandbox): they return commands as data, and the
platform runs them here through :meth:`SystemDevice.execute`, after the checks.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable

from pydantic import AnyHttpUrl

from factotum.contracts.audio import AudioPayload
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import (
    AdjustVolume,
    ClearDisplay,
    CommandResult,
    DeviceCommand,
    LedPattern,
    MediaKind,
    Pause,
    PlayAudio,
    PlayUrl,
    Resume,
    SetLed,
    SetVolume,
    ShowCard,
    Speak,
    Stop,
    VolumeDirection,
)
from factotum.contracts.errors import CapabilityNotSupportedError
from factotum.contracts.ids import DeviceId


class SystemDevice(ABC):
    def __init__(self, device_id: DeviceId, capabilities: Iterable[Capability]) -> None:
        self._device_id = device_id
        self._capabilities = frozenset(capabilities)

    @property
    def device_id(self) -> DeviceId:
        return self._device_id

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def supports(self, capability: Capability) -> bool:
        return capability in self._capabilities

    @abstractmethod
    async def send(self, command: DeviceCommand) -> CommandResult:
        """Deliver an already checked command to the device and report what happened."""

    async def execute(self, command: DeviceCommand) -> CommandResult:
        """Run any command of the vocabulary, provided the device supports it.

        Raises ``CapabilityNotSupportedError`` when the device did not declare the capability the
        command requires; nothing is sent in that case.
        """
        if not self.supports(command.requires):
            raise CapabilityNotSupportedError(
                f"device {self._device_id} does not support {command.requires.value!r}, "
                f"which {command.name!r} needs",
                details={"capability": command.requires.value, "command": command.name},
            )
        return await self.send(command)

    # --- ready-made methods: one per command of the vocabulary ---------------------------------

    async def speak(self, text: str, *, language: str | None = None) -> CommandResult:
        return await self.execute(Speak(text=text, language=language))

    async def play_audio(self, text: str, audio: AudioPayload) -> CommandResult:
        """Have the device say ``text`` with a clip of it that was already made (ADR 0026)."""
        return await self.execute(PlayAudio(text=text, audio=audio))

    async def play_url(
        self, url: str, *, kind: MediaKind = MediaKind.AUDIO, title: str | None = None
    ) -> CommandResult:
        return await self.execute(PlayUrl(url=AnyHttpUrl(url), kind=kind, title=title))

    async def pause(self) -> CommandResult:
        return await self.execute(Pause())

    async def resume(self) -> CommandResult:
        return await self.execute(Resume())

    async def stop(self) -> CommandResult:
        return await self.execute(Stop())

    async def set_volume(self, level: int) -> CommandResult:
        return await self.execute(SetVolume(level=level))

    async def adjust_volume(
        self, direction: VolumeDirection, amount: int | None = None
    ) -> CommandResult:
        return await self.execute(AdjustVolume(direction=direction, amount=amount))

    async def show_card(self, card: Card) -> CommandResult:
        return await self.execute(ShowCard(card=card))

    async def clear_display(self) -> CommandResult:
        return await self.execute(ClearDisplay())

    async def set_led(self, pattern: LedPattern, color: str | None = None) -> CommandResult:
        return await self.execute(SetLed(pattern=pattern, color=color))
