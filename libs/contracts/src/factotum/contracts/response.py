"""What the conversation service sends back: typed parts, not just text.

Each part says which capability a device needs to render it, so the edge can adapt one answer to
devices that are as different as a voice-only speaker and a screen without a microphone.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, Field

from factotum.contracts.audio import AudioPayload
from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import DeviceCommand, MediaKind


class StatusState(StrEnum):
    THINKING = "thinking"
    SEARCHING = "searching"
    ACTING = "acting"


class Speech(Contract):
    """Something to say aloud."""

    type: Literal["speech"] = "speech"
    text: str = Field(min_length=1, max_length=2000)
    audio: AudioPayload | None = None
    """``text`` synthesized, when a text-to-speech role is bound. ``None`` means the device must
    speak the words itself, or not at all: the text is sent either way."""


class Text(Contract):
    """Something to show as plain text."""

    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=4000)


class CardPart(Contract):
    type: Literal["card"] = "card"
    card: Card


class MediaPart(Contract):
    type: Literal["media"] = "media"
    url: AnyHttpUrl
    kind: MediaKind = MediaKind.AUDIO
    title: str | None = Field(default=None, max_length=200)


class CommandPart(Contract):
    """A command for the device itself (set the volume, pause the music...)."""

    type: Literal["command"] = "command"
    command: DeviceCommand


class StatusPart(Contract):
    """A progress hint (thinking, searching, acting) that any device may ignore."""

    type: Literal["status"] = "status"
    state: StatusState


PartUnion = Speech | Text | CardPart | MediaPart | CommandPart | StatusPart

ResponsePart = Annotated[PartUnion, Field(discriminator="type")]


def required_capability(part: PartUnion) -> Capability | None:
    """The capability a device needs to render ``part``, or None when any device can."""
    match part:
        case Speech():
            return Capability.AUDIO_OUT
        case Text() | CardPart():
            return Capability.DISPLAY
        case MediaPart():
            return Capability.MEDIA_PLAYER
        case CommandPart():
            return part.command.requires
        case StatusPart():
            return None
