"""The hardware of the device the terminal pretends to be, as far as commands go.

What a command does to a real device is done here to a few numbers and a note of it: the volume is
a number, the light has a pattern and a colour, the music is playing, paused or stopped, the screen
has a card on it. That is enough to see the edge work, and to see that a device answers a command
with what became of it, which is the point of the acknowledgement.
"""

from dataclasses import dataclass, field

from factotum.client_core.ports import Renderer
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import (
    AdjustVolume,
    ClearDisplay,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    LedPattern,
    Pause,
    PlayUrl,
    Resume,
    SetLed,
    SetVolume,
    ShowCard,
    Speak,
    Stop,
)


@dataclass
class DeviceState:
    """What the hardware is doing."""

    volume: int = 50
    led_pattern: LedPattern = LedPattern.OFF
    led_color: str | None = None
    media: str = "stopped"
    """``stopped``, ``playing`` or ``paused``."""

    media_title: str | None = None
    card: Card | None = None
    spoken: list[str] = field(default_factory=list)

    def describe(self) -> str:
        light = self.led_pattern.value + (f" {self.led_color}" if self.led_color else "")
        media = self.media + (f" ({self.media_title})" if self.media_title else "")
        card = f"a card: {self.card.title}" if self.card else "nothing"
        return f"volume {self.volume}; light {light}; music {media}; screen: {card}"


class TerminalDevice:
    """The hands of a device in a terminal: a ``factotum.client_core.CommandDriver``."""

    def __init__(self, capabilities: frozenset[Capability], renderer: Renderer) -> None:
        self._capabilities = capabilities
        self._renderer = renderer
        self.state = DeviceState()

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    async def execute(self, command: DeviceCommand) -> CommandResult:
        """Run a command, and say what became of it."""
        if command.requires not in self._capabilities:
            return CommandResult(
                status=CommandStatus.UNSUPPORTED,
                detail=f"this device has no {command.requires.value!r}",
            )
        state = self.state
        match command:
            case Speak():
                state.spoken.append(command.text)
                self._renderer.speech(command.text)
            case PlayUrl():
                state.media, state.media_title = "playing", command.title or str(command.url)
                self._renderer.media(command.title, str(command.url))
            case Pause():
                if state.media != "playing":
                    return _failed("nothing is playing")
                state.media = "paused"
                self._renderer.event("music paused")
            case Resume():
                if state.media != "paused":
                    return _failed("nothing is paused")
                state.media = "playing"
                self._renderer.event("music resumed")
            case Stop():
                state.media, state.media_title = "stopped", None
                self._renderer.event("music stopped")
            case SetVolume():
                state.volume = command.level
                self._renderer.event(f"volume {command.level}")
            case AdjustVolume():
                state.volume = max(0, min(state.volume + command.delta, 100))
                self._renderer.event(f"volume {state.volume}")
            case ShowCard():
                state.card = command.card
                self._renderer.card(command.card)
            case ClearDisplay():
                state.card = None
                self._renderer.event("screen cleared")
            case SetLed():
                state.led_pattern, state.led_color = command.pattern, command.color
                colour = f" {command.color}" if command.color else ""
                self._renderer.event(f"light {command.pattern.value}{colour}")
            case _:
                # A clip is played by the client core, which has the speaker; these hands are
                # never asked to, and what they were not made for is not said to have been done.
                return CommandResult(
                    status=CommandStatus.UNSUPPORTED,
                    detail=f"this device has no hands for {command.name!r}",
                )
        return CommandResult.ok()


def _failed(detail: str) -> CommandResult:
    return CommandResult(status=CommandStatus.FAILED, detail=detail)
