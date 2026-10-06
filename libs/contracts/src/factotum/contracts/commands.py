"""The vocabulary of commands a system device understands (version 2).

This is the single definition every other piece derives from: the server-side handle
(``factotum.devices.SystemDevice``), the command builders offered to skills and the drivers that
run on the devices. A device only ever receives commands it declared it can execute (see
``BaseCommand.requires``) and never receives code.

What a device may be sent (``DeviceCommand``) is not what a skill may give it (``SkillCommand``): a
clip to play (``play_audio``, ADR 0026) is the platform's, and a script or a plan cannot have one.
"""

from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import AnyHttpUrl, Field

from factotum.contracts.audio import AudioPayload
from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card

COMMAND_VOCABULARY_VERSION = 2
"""Version 2 added ``play_audio``."""

MAX_SPOKEN_CHARS = 2000
"""The most a command says: a text to speak, or the words of a clip."""


class MediaKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"


class LedPattern(StrEnum):
    OFF = "off"
    SOLID = "solid"
    BLINK = "blink"
    PULSE = "pulse"


class BaseCommand(Contract):
    """A command; ``requires`` is the capability the target device must have declared."""

    requires: ClassVar[Capability]


class Speak(BaseCommand):
    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["speak"] = "speak"
    text: str = Field(min_length=1, max_length=MAX_SPOKEN_CHARS)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")


class PlayAudio(BaseCommand):
    """Say something with a clip that was made for it (ADR 0026): a whole clip of 16-bit mono PCM,
    and the words that it says. The device is not to say the words: they are for the trail of what
    was done (a clip is opaque and a text is not), for a device that shows what is said, and for the
    edge to say, or show, in place of a clip that a device cannot play.

    The platform makes it (a core skill, or the edge for a ``speak``); a skill does not."""

    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["play_audio"] = "play_audio"
    text: str = Field(min_length=1, max_length=MAX_SPOKEN_CHARS)
    audio: AudioPayload


class PlayUrl(BaseCommand):
    requires: ClassVar[Capability] = Capability.MEDIA_PLAYER

    name: Literal["play_url"] = "play_url"
    url: AnyHttpUrl
    kind: MediaKind = MediaKind.AUDIO
    title: str | None = Field(default=None, max_length=200)


class Pause(BaseCommand):
    requires: ClassVar[Capability] = Capability.MEDIA_PLAYER

    name: Literal["pause"] = "pause"


class Resume(BaseCommand):
    requires: ClassVar[Capability] = Capability.MEDIA_PLAYER

    name: Literal["resume"] = "resume"


class Stop(BaseCommand):
    requires: ClassVar[Capability] = Capability.MEDIA_PLAYER

    name: Literal["stop"] = "stop"


class SetVolume(BaseCommand):
    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["set_volume"] = "set_volume"
    level: int = Field(ge=0, le=100)


class ShowCard(BaseCommand):
    requires: ClassVar[Capability] = Capability.DISPLAY

    name: Literal["show_card"] = "show_card"
    card: Card


class ClearDisplay(BaseCommand):
    requires: ClassVar[Capability] = Capability.DISPLAY

    name: Literal["clear_display"] = "clear_display"


class SetLed(BaseCommand):
    requires: ClassVar[Capability] = Capability.LED

    name: Literal["set_led"] = "set_led"
    pattern: LedPattern
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


COMMAND_MODELS: tuple[type[BaseCommand], ...] = (
    Speak,
    PlayAudio,
    PlayUrl,
    Pause,
    Resume,
    Stop,
    SetVolume,
    ShowCard,
    ClearDisplay,
    SetLed,
)

DeviceCommand = Annotated[
    Speak
    | PlayAudio
    | PlayUrl
    | Pause
    | Resume
    | Stop
    | SetVolume
    | ShowCard
    | ClearDisplay
    | SetLed,
    Field(discriminator="name"),
]
"""What a device may be sent."""

SKILL_COMMAND_MODELS: tuple[type[BaseCommand], ...] = tuple(
    model for model in COMMAND_MODELS if model is not PlayAudio
)

SkillCommand = Annotated[
    Speak | PlayUrl | Pause | Resume | Stop | SetVolume | ShowCard | ClearDisplay | SetLed,
    Field(discriminator="name"),
]
"""What a skill may give a device: the vocabulary but for what only the platform makes."""


class CommandStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    OFFLINE = "offline"


class CommandResult(Contract):
    """What a device (or the layer in front of it) reports after a command."""

    status: CommandStatus
    detail: str | None = Field(default=None, max_length=500)

    @classmethod
    def ok(cls) -> "CommandResult":
        return cls(status=CommandStatus.OK)

    @property
    def succeeded(self) -> bool:
        return self.status is CommandStatus.OK
