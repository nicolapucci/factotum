"""What a device answers about its timers and its alarms (ADR 0029).

A device keeps the time on a clock of its own, so what it says of a timer or an alarm is the answer
to a command (``set_timer``, ``cancel_alarm``, ``get_state``, ...), in the ``data`` of the result.
The skill of the platform that gave the command reads it in code and says it in a sentence of its
own: a model is never given what a device answered. These are the shapes of it, defined once, so
that the device that writes an answer and the skill that reads it cannot disagree about what is in
it.

A moment on the wall is *civil time*, the time that the clock of the place the device is in shows
(``2026-10-14T07:00:00``), with no zone: that is what a person means by "at seven", it is what an
alarm is kept as (it rings when the wall says so, whatever the clock did in between: summer time, a
journey), and it is all that the skill needs to say "tomorrow at seven". The platform has no clock
and never works one out.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from factotum.contracts.base import Contract

MAX_TIMERS = 8
"""The most timers a device holds at once. A device refuses another (``too_many``)."""

MAX_ALARMS = 8
"""The most alarms a device holds at once."""

MAX_ID_CHARS = 32


def _is_civil(moment: datetime) -> datetime:
    if moment.tzinfo is not None:
        raise ValueError("a moment on the wall of a device has no zone")
    return moment


class TimerInfo(Contract):
    """A timer of a device."""

    id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    seconds: int = Field(ge=1)
    """How long it was set for."""

    left_s: int = Field(ge=0)
    """How long is left; nothing when it is ringing."""

    ringing: bool = False


class AlarmInfo(Contract):
    """An alarm of a device."""

    id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    at: datetime
    """When it rings, on the wall of the device (civil time)."""

    ringing: bool = False

    @field_validator("at")
    @classmethod
    def _at_is_civil(cls, moment: datetime) -> datetime:
        return _is_civil(moment)


class TimeState(Contract):
    """What a device says of its time when it is asked how it is (``get_state``). It is a part of
    the answer: the volume is there too, and it is not this part's."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    now: datetime
    """What the wall of the device shows (civil time): what "today" is, for the skill that says an
    alarm."""

    timers: tuple[TimerInfo, ...] = ()
    alarms: tuple[AlarmInfo, ...] = ()

    @field_validator("now")
    @classmethod
    def _now_is_civil(cls, moment: datetime) -> datetime:
        return _is_civil(moment)


class TimerSet(Contract):
    """What a device answers to ``set_timer``: the timer it set, or why it set none."""

    timer: TimerInfo | None = None
    reason: Literal["too_many"] | None = None

    @model_validator(mode="after")
    def _is_one_or_the_other(self) -> Self:
        if (self.timer is None) == (self.reason is None):
            raise ValueError("a timer was set, or it says why not")
        return self


class TimersCancelled(Contract):
    """What a device answers to ``cancel_timer``: the timers it stopped (none, when it had none to
    stop, or none that was named) and how many are left."""

    cancelled: tuple[TimerInfo, ...] = ()
    remaining: int = Field(ge=0)


class AlarmSet(Contract):
    """What a device answers to ``set_alarm``: the alarm it set, at the moment it chose, or why it
    set none: a moment that has gone by (``past``), a date that never comes (``no_such_day``: the
    31st of April), a device that holds as many as it can (``too_many``)."""

    now: datetime
    alarm: AlarmInfo | None = None
    reason: Literal["past", "no_such_day", "too_many"] | None = None

    @field_validator("now")
    @classmethod
    def _now_is_civil(cls, moment: datetime) -> datetime:
        return _is_civil(moment)

    @model_validator(mode="after")
    def _is_one_or_the_other(self) -> Self:
        if (self.alarm is None) == (self.reason is None):
            raise ValueError("an alarm was set, or it says why not")
        return self


class AlarmsCancelled(Contract):
    """What a device answers to ``cancel_alarm``."""

    now: datetime
    cancelled: tuple[AlarmInfo, ...] = ()
    remaining: int = Field(ge=0)

    @field_validator("now")
    @classmethod
    def _now_is_civil(cls, moment: datetime) -> datetime:
        return _is_civil(moment)


def data_of(answer: Contract) -> dict[str, JsonValue]:
    """An answer as the ``data`` of a result."""
    return answer.model_dump(mode="json")


def answer_in[T: Contract](shape: type[T], data: Mapping[str, object] | None) -> T | None:
    """The answer a device gave, as the shape it should have, or nothing when it is not that: a
    device that answers what a skill cannot read is a device that did not answer."""
    try:
        return shape.model_validate(data or {})
    except ValidationError:
        return None
