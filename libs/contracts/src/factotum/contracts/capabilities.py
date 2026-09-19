"""What a system device can do. A device declares its capabilities when it connects."""

from enum import StrEnum

from pydantic import Field

from factotum.contracts.base import Contract


class Capability(StrEnum):
    AUDIO_IN = "audio_in"
    """Microphone: the device can start conversations."""

    AUDIO_OUT = "audio_out"
    """Speaker: the device can speak and play sound."""

    DISPLAY = "display"
    """Screen: the device can show text and cards."""

    MEDIA_PLAYER = "media_player"
    """The device can play a URL or a stream and be paused, resumed and stopped."""

    BUTTONS = "buttons"
    LED = "led"
    SENSORS = "sensors"


class DisplayInfo(Contract):
    """The screen of a device that declares the ``display`` capability."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    touch: bool = False
