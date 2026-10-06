"""What a device does when it is told to: the hands of a speaker.

A command that the edge sends is one that this device declared it can run (ADR 0013): a speaker with
a microphone declares ``audio_in`` and ``audio_out``, and what ``audio_out`` means in the vocabulary
of commands is a volume to set and a text to say. The volume is the speaker's own (the samples are
scaled before they are played: ``factotum.client_audio.Speaker``), and so it is the same on a PC and
on a Raspberry Pi.

A text to say (``speak``, which the ``announce`` skill answers with) is what this device cannot do
yet: it has no voice of its own, and the voice is made by the gateway, which the edge reaches and a
device does not. It says so, and the skill is told that it did not work, which is the truth: a
device that showed the text and answered ``ok`` would be an announcement that nobody heard and the
trail says was done. What the edge would have to do is make that voice and stream it, as it does
for an answer (a decision of its own, which is not made here).
"""

from factotum.client_audio import Speaker
from factotum.client_core.ports import Renderer
from factotum.contracts.capabilities import Capability
from factotum.contracts.commands import (
    CommandResult,
    CommandStatus,
    DeviceCommand,
    SetVolume,
    Speak,
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
            case Speak():
                self._renderer.event("asked to say a text, which this device cannot yet")
                return CommandResult(
                    status=CommandStatus.UNSUPPORTED,
                    detail="this device has no voice of its own to say a text with",
                )
        return CommandResult(
            status=CommandStatus.UNSUPPORTED,
            detail=f"this device has no hands for {command.name!r}",
        )
