"""What a device does when it is told to: the hands of a speaker.

A command that the edge sends is one that this device declared it can run (ADR 0013): a speaker with
a microphone declares ``audio_in`` and ``audio_out``, and what the hands of ``audio_out`` do in the
vocabulary of commands is set the volume, or change it by some points. The volume is the speaker's
own (the samples are scaled before they are played: ``factotum.client_audio.Speaker``), and so it is
the same on a PC and on a Raspberry Pi.

What is said is not the hands' work. A clip (``play_audio``, which the ``announce`` skill of the
platform answers with) is played by the client core, which has the speaker and the sound of what is
being answered to wait for; and a text to say (``speak``, which any skill may answer with) never
reaches a device that said it plays clips, because the edge makes the voice of it and sends that
(ADR 0026). So what is left of those two for these hands is the answer that a device gives to a
command it has no hands for: it says so, and the skill is told that it did not work, which is the
truth.
"""

from factotum.client_audio import Speaker
from factotum.client_core.ports import Renderer
from factotum.contracts.capabilities import Capability
from factotum.contracts.commands import (
    AdjustVolume,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    SetVolume,
)


class DeviceHands:
    """A ``factotum.client_core.CommandDriver`` for what this application is made of."""

    def __init__(
        self, capabilities: frozenset[Capability], speaker: Speaker | None, renderer: Renderer
    ) -> None:
        self._capabilities = capabilities
        self._speaker = speaker
        self._renderer = renderer

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    async def execute(self, command: DeviceCommand) -> CommandResult:
        """Run a command, and say what became of it."""
        match command:
            case SetVolume() if self._speaker is not None:
                self._speaker.set_volume(command.level)
                self._renderer.event(f"volume {command.level}")
                return CommandResult.ok()
            case AdjustVolume() if self._speaker is not None:
                self._renderer.event(f"volume {self._speaker.adjust_volume(command.delta)}")
                return CommandResult.ok()
        return CommandResult(
            status=CommandStatus.UNSUPPORTED,
            detail=f"this device has no hands for {command.name!r}",
        )
