"""The vocabulary of commands a system device understands (version 5).

This is the single definition every other piece derives from: the server-side handle
(``factotum.devices.SystemDevice``), the command builders offered to skills and the drivers that
run on the devices. A device only ever receives commands it declared it can execute (see
``BaseCommand.requires``) and never receives code.

What a device may be sent (``DeviceCommand``) is not what a skill may give it (``SkillCommand``): a
clip to play (``play_audio``, ADR 0026) is the platform's, and a script or a plan cannot have one;
nor can they ask a device a question (``get_state``, ADR 0029), because the answer is for the skill
of the platform that asked it and a script, or a model, is never given what a device answered. The
timers and the alarms of a device (ADR 0029) are the platform's too: they carry the clip that rings.
"""

import json
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self

from pydantic import AnyHttpUrl, Field, JsonValue, field_validator, model_validator

from factotum.contracts.audio import AudioPayload
from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card

COMMAND_VOCABULARY_VERSION = 5
"""Version 2 added ``play_audio``, version 3 added ``adjust_volume``, version 4 added ``set_mute``
and ``get_state``, and the ``data`` that the result of a command may carry, and version 5 added the
timers and the alarms of a device: ``set_timer``, ``cancel_timer``, ``set_alarm`` and
``cancel_alarm``."""

MAX_SPOKEN_CHARS = 2000
"""The most a command says: a text to speak, or the words of a clip."""

MAX_RESULT_DATA_CHARS = 4000
"""The most that the answer of a device to a question may be, as the JSON that it is sent as."""

MAX_TIMER_SECONDS = 24 * 60 * 60
"""The longest a timer is set for: a day. A person who wants it for longer wants an alarm."""


class MediaKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"


class LedPattern(StrEnum):
    OFF = "off"
    SOLID = "solid"
    BLINK = "blink"
    PULSE = "pulse"


class VolumeDirection(StrEnum):
    UP = "up"
    DOWN = "down"


class Weekday(StrEnum):
    """A day of the week, in the order of the calendar's week (Monday first)."""

    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class AlarmDay(Contract):
    """A way of saying a day, as a person says it, and not a date (ADR 0029): the platform has no
    calendar and no clock, and the device that has both (and the time zone of the place it is in)
    turns it into the day it means.

    Nothing said is the next time that the hour comes. ``in_days`` counts from today (0 is today,
    1 is tomorrow, 2 the day after). ``weekday`` is the next such day of the week (today, when the
    hour is still to come). ``day`` is a day of the month, in ``month`` if one was said: the next
    time that date comes."""

    in_days: int | None = Field(default=None, ge=0, le=366)
    weekday: Weekday | None = None
    day: int | None = Field(default=None, ge=1, le=31)
    month: int | None = Field(default=None, ge=1, le=12)

    @model_validator(mode="after")
    def _is_said_one_way(self) -> Self:
        said = (self.in_days is not None, self.weekday is not None, self.day is not None)
        if sum(said) > 1:
            raise ValueError("a day is said one way: in some days, a weekday or a date")
        if self.month is not None and self.day is None:
            raise ValueError("a month is a month of a day")
        return self


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


class AdjustVolume(BaseCommand):
    """Make the volume louder or quieter than it is (ADR 0028).

    A ``set_volume`` says which level; this says which way, which is what "turn it up" means and
    needs no knowledge of where the volume is. ``amount`` is how many points of the volume, when a
    person said it. When they did not, the device turns it by its own step: a speaker is not a
    television, and what a step is belongs to the device that has the volume. The skill that gives
    the command is the interface between a model and the device, and has no step to tell. The level
    that results stays between 0 and 100."""

    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["adjust_volume"] = "adjust_volume"
    direction: VolumeDirection
    amount: int | None = Field(default=None, ge=1, le=100)


class SetMute(BaseCommand):
    """Mute the sound of the device, or bring it back (ADR 0029). The device keeps the level that
    the volume was at, so that unmuting brings back what it was; setting or turning the volume
    brings the sound back too, as it does on a television."""

    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["set_mute"] = "set_mute"
    muted: bool


class GetState(BaseCommand):
    """Ask a device how it is (ADR 0029): the answer is in the ``data`` of the result, for the skill
    of the platform that asked. A device that has a volume says ``volume`` (0 to 100) and
    ``muted``, and what it keeps for later (timers, alarms) is there too. It changes nothing."""

    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["get_state"] = "get_state"


class SetTimer(BaseCommand):
    """Have the device ring after ``seconds`` (ADR 0029). The device keeps the time on its own clock
    and rings by itself when it is up, even if its connection is down then, with the clip that was
    made when the timer was set (``text`` is the words of it, for the trail and for a device that
    shows what it says), and goes on until it is stopped. It answers with the timer it set.

    The platform's: a clip is sound that nobody but the platform makes (ADR 0026)."""

    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["set_timer"] = "set_timer"
    seconds: int = Field(ge=1, le=MAX_TIMER_SECONDS)
    text: str = Field(min_length=1, max_length=MAX_SPOKEN_CHARS)
    audio: AudioPayload


class CancelTimer(BaseCommand):
    """Stop a timer of the device, or the ring of one that is up (ADR 0029). The one named by its
    length in ``seconds`` if a person said it; else the one that is ringing; else the next to ring.
    What the device did is in its answer."""

    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["cancel_timer"] = "cancel_timer"
    seconds: int | None = Field(default=None, ge=1, le=MAX_TIMER_SECONDS)


class SetAlarm(BaseCommand):
    """Have the device ring at an hour of the day (ADR 0029): ``hour`` and ``minute`` as the person
    said them (24 hours), on ``day`` as they said it. The device has the clock and the calendar of
    the place it is in: it works out the next time that fits, and answers with the moment it chose
    (``at``, with the ``now`` that it chose it from) so that the skill can say it. A moment that has
    gone by is not an alarm: it answers that, and sets nothing. It rings, as a timer does, with a
    clip that was made when the alarm was set.

    The platform's, for the same reason as ``set_timer``."""

    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["set_alarm"] = "set_alarm"
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    day: AlarmDay = Field(default_factory=AlarmDay)
    text: str = Field(min_length=1, max_length=MAX_SPOKEN_CHARS)
    audio: AudioPayload


class CancelAlarm(BaseCommand):
    """Stop an alarm of the device, or the ring of one that is up (ADR 0029). The one at ``hour``
    and ``minute`` if a person said it; else the one that is ringing; else the next to ring."""

    requires: ClassVar[Capability] = Capability.AUDIO_OUT

    name: Literal["cancel_alarm"] = "cancel_alarm"
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)

    @model_validator(mode="after")
    def _is_an_hour_and_a_minute(self) -> Self:
        if (self.hour is None) != (self.minute is None):
            raise ValueError("an alarm is named by its hour and its minute together")
        return self


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
    AdjustVolume,
    SetMute,
    GetState,
    SetTimer,
    CancelTimer,
    SetAlarm,
    CancelAlarm,
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
    | AdjustVolume
    | SetMute
    | GetState
    | SetTimer
    | CancelTimer
    | SetAlarm
    | CancelAlarm
    | ShowCard
    | ClearDisplay
    | SetLed,
    Field(discriminator="name"),
]
"""What a device may be sent."""

PLATFORM_COMMAND_MODELS: tuple[type[BaseCommand], ...] = (
    PlayAudio,
    GetState,
    SetTimer,
    CancelTimer,
    SetAlarm,
    CancelAlarm,
)
"""What only the skills of the platform may give: a clip that nobody but the platform makes (a ring
carries one), and what a device answers to a question or says of a timer, which a script or a model
is never given."""

SKILL_COMMAND_MODELS: tuple[type[BaseCommand], ...] = tuple(
    model for model in COMMAND_MODELS if model not in PLATFORM_COMMAND_MODELS
)

SkillCommand = Annotated[
    Speak
    | PlayUrl
    | Pause
    | Resume
    | Stop
    | SetVolume
    | AdjustVolume
    | SetMute
    | ShowCard
    | ClearDisplay
    | SetLed,
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

    data: dict[str, JsonValue] | None = None
    """What a device answers to a question, or says of what it did (the level the volume came to):
    small, and for the skill of the platform that gave the command (ADR 0029). A model never reads
    it, and a script never gets it."""

    @field_validator("data")
    @classmethod
    def _is_small(cls, data: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if data is not None and len(json.dumps(data)) > MAX_RESULT_DATA_CHARS:
            raise ValueError(
                f"the answer of a device is at most {MAX_RESULT_DATA_CHARS} characters"
            )
        return data

    @classmethod
    def ok(cls, data: dict[str, JsonValue] | None = None) -> "CommandResult":
        return cls(status=CommandStatus.OK, data=data)

    @property
    def succeeded(self) -> bool:
        return self.status is CommandStatus.OK
