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
    AlarmDay,
    CancelAlarm,
    CancelTimer,
    ClearDisplay,
    CommandResult,
    DeviceCommand,
    GetState,
    LedPattern,
    MediaKind,
    Pause,
    PlayAudio,
    PlayUrl,
    Resume,
    SetAlarm,
    SetLed,
    SetMute,
    SetTimer,
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

    async def set_mute(self, muted: bool) -> CommandResult:
        return await self.execute(SetMute(muted=muted))

    async def get_state(self) -> CommandResult:
        """Ask the device how it is: the answer is in the ``data`` of the result."""
        return await self.execute(GetState())

    async def set_timer(self, seconds: int, text: str, audio: AudioPayload) -> CommandResult:
        """Have the device ring after ``seconds`` with a clip of ``text`` (ADR 0029): the device
        keeps the time, and answers with the timer it set."""
        return await self.execute(SetTimer(seconds=seconds, text=text, audio=audio))

    async def cancel_timer(self, seconds: int | None = None) -> CommandResult:
        """Stop a timer of the device: the one of that length, else the one that is ringing, else
        the next to ring."""
        return await self.execute(CancelTimer(seconds=seconds))

    async def set_alarm(
        self,
        hour: int,
        minute: int,
        text: str,
        audio: AudioPayload,
        *,
        day: AlarmDay | None = None,
    ) -> CommandResult:
        """Have the device ring at ``hour`` and ``minute`` on ``day`` (the next time they come, when
        there is none): the device works out the moment from its own clock and calendar, and answers
        with the one it chose."""
        return await self.execute(
            SetAlarm(hour=hour, minute=minute, day=day or AlarmDay(), text=text, audio=audio)
        )

    async def cancel_alarm(
        self, hour: int | None = None, minute: int | None = None
    ) -> CommandResult:
        """Stop an alarm of the device: the one at that hour and minute, else the one that is
        ringing, else the next to ring."""
        return await self.execute(CancelAlarm(hour=hour, minute=minute))

    async def show_card(self, card: Card) -> CommandResult:
        return await self.execute(ShowCard(card=card))

    async def clear_display(self) -> CommandResult:
        return await self.execute(ClearDisplay())

    async def set_led(self, pattern: LedPattern, color: str | None = None) -> CommandResult:
        return await self.execute(SetLed(pattern=pattern, color=color))
