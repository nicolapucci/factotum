"""The skills of the platform for the alarms of a device (ADR 0029): one is set, cancelled, and
asked about.

Each is the interface between a model and the clock of a device, and the device has the clock and
the calendar of the place it is in: the skill says only *set the alarm for this hour on this day*.
Toward the model it says what can be asked: the *words* of the hour and of the day (*alle sette e
mezza*, *domani*, *lunedì*, *il 15 ottobre*), which the check of ADR 0028 finds in the message.
Toward the device it passes on the hour and the minute that code reads in them
(``when.parse_alarm``) and the day as the person said it, **never a date**: the platform has no
clock, so it cannot know what day *tomorrow* is, and the device, which does, works out the next
time that fits and answers with the moment it chose. That answer is read here, in code, and said in
a sentence of ``phrases`` in the language of the person: a model never reads it.
"""

from collections.abc import Sequence
from typing import ClassVar

from factotum.actions.core.phrases import (
    CoreSay,
    core_say,
    say_clock,
    say_list,
    say_when,
)
from factotum.actions.core.rings import ring_of
from factotum.actions.core.skills import CoreResult, CoreSkill, SpeechMaker
from factotum.actions.core.when import AlarmWords, parse_alarm
from factotum.contracts.commands import AlarmDay, CancelAlarm, CommandResult, GetState, SetAlarm
from factotum.contracts.errors import SkillFailedError, SkillRunError
from factotum.contracts.identity import Principal
from factotum.contracts.runner import RunRequest
from factotum.contracts.timekeeping import AlarmsCancelled, AlarmSet, TimeState, answer_in

ALARM_SOURCE = """\
---
name: alarm
description: >-
  Set an alarm on the device that is being spoken to, for an hour of the day and for a day if the
  person says one. The device rings by itself at that hour.
examples:
  - set an alarm for seven thirty
  - wake me up at 6 tomorrow
  - set an alarm for monday at eight
  - alarm at half past six
  - metti la sveglia alle sette e mezza
  - svegliami domani alle 6
  - imposta una sveglia per lunedì alle otto
target: system
parameters:
  time:
    type: string
    description: >-
      the hour of the day, in the words the person said (seven thirty, 6 pm, alle sette e mezza)
  day:
    type: string
    description: >-
      the day, in the words the person said (tomorrow, monday, the 15th of october, domani), when
      they say one (leave it out when they do not)
required: [time]
effect: reversible
timeout_s: 60
---
A skill of the platform: the interface between a model and the alarms of a device. The words of the
hour and of the day are read by code (a model does no sums), the voice that rings is made once by
the model gateway when the alarm is set, and the device, which has the clock and the calendar of
the place it is in, works out which day it is and rings by itself.
"""

ALARM_CANCEL_SOURCE = """\
---
name: alarm-cancel
description: >-
  Cancel an alarm on the device that is being spoken to, or stop one that is ringing: the one at
  the hour the person names, or else the one that is ringing, or else the next one to ring.
examples:
  - cancel the alarm
  - stop the alarm
  - turn off the alarm
  - cancel the seven thirty alarm
  - cancella la sveglia
  - spegni la sveglia
  - annulla la sveglia delle sette
target: system
parameters:
  time:
    type: string
    description: >-
      the hour of the alarm to cancel, in the words the person said, when they say which one
      (leave it out when they do not)
effect: reversible
---
A skill of the platform: the interface between a model and the alarms of a device. It passes on
which alarm the person named, by its hour, if they did; which one it is when they did not is the
device's to choose (the one that rings, else the next to ring).
"""

ALARM_STATUS_SOURCE = """\
---
name: alarm-status
description: >-
  Say what alarms are set on the device that is being spoken to, and when the next one rings.
examples:
  - what alarms do I have
  - when is my alarm
  - is there an alarm set
  - a che ora suona la sveglia
  - quali sveglie ho
  - c'è una sveglia impostata
target: system
effect: read_only
---
A skill of the platform: it asks the device how it is and says what the device answered, in the
language of the person. It changes nothing.
"""


def _alarm_of(words: str, language: str) -> AlarmWords:
    """The hour and the day that the words say, or the skill says that it did not understand."""
    alarm = parse_alarm(words)
    if alarm is None:
        raise SkillFailedError(core_say(language, CoreSay.TIME_UNCLEAR))
    return alarm


class Alarm(CoreSkill):
    source: ClassVar[str] = ALARM_SOURCE

    def __init__(self, speech: SpeechMaker) -> None:
        super().__init__()
        self._speech = speech

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        hour = request.params.get("time")
        day = request.params.get("day")
        if not isinstance(hour, str) or not (day is None or isinstance(day, str)):
            raise SkillFailedError(core_say(language, CoreSay.TIME_UNCLEAR))
        alarm = _alarm_of(f"{day or ''} {hour}", language)
        ring = core_say(language, CoreSay.ALARM_RING)
        audio = await ring_of(self._speech, principal, ring)

        def said(results: Sequence[CommandResult]) -> str | None:
            """The alarm that the device set and the moment it chose, as it says them: a moment that
            cannot be, a date that never comes and a device that is full are said, and one that does
            not answer is a skill that failed."""
            answer = answer_in(AlarmSet, results[0].data) if results else None
            if answer is None:
                raise SkillRunError("the device did not say which alarm it set")
            if answer.alarm is None:
                reasons = {
                    "past": CoreSay.ALARM_PAST,
                    "no_such_day": CoreSay.ALARM_NO_SUCH_DAY,
                    "too_many": CoreSay.ALARMS_FULL,
                }
                raise SkillFailedError(core_say(language, reasons[answer.reason or "past"]))
            when = say_when(language, answer.alarm.at, answer.now)
            return core_say(language, CoreSay.ALARM_SET, when=when)

        return CoreResult(
            commands=(
                SetAlarm(
                    hour=alarm.hour,
                    minute=alarm.minute,
                    day=alarm.day,
                    text=ring,
                    audio=audio,
                ),
            ),
            audit=f"alarm set for {alarm.hour:02d}:{alarm.minute:02d} ({_said_day(alarm.day)})",
            after=said,
        )


def _said_day(day: AlarmDay) -> str:
    """How the day was said, for the trail: the platform does not know which day it is."""
    fields = day.model_dump(exclude_none=True)
    return ", ".join(f"{name} {value}" for name, value in fields.items()) or "the next time"


class AlarmCancel(CoreSkill):
    source: ClassVar[str] = ALARM_CANCEL_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        named = request.params.get("time")
        alarm = None if named is None else _alarm_of(str(named), language)
        command = (
            CancelAlarm() if alarm is None else CancelAlarm(hour=alarm.hour, minute=alarm.minute)
        )

        def said(results: Sequence[CommandResult]) -> str | None:
            answer = answer_in(AlarmsCancelled, results[0].data) if results else None
            if answer is None:
                raise SkillRunError("the device did not say which alarm it cancelled")
            if not answer.cancelled:
                if alarm is None:
                    return core_say(language, CoreSay.ALARM_NONE)
                at = say_clock(language, alarm.hour, alarm.minute)
                return core_say(language, CoreSay.ALARM_NONE_NAMED, at=at)
            if len(answer.cancelled) > 1:
                return core_say(language, CoreSay.ALARMS_CANCELLED, count=len(answer.cancelled))
            when = say_when(language, answer.cancelled[0].at, answer.now)
            return core_say(language, CoreSay.ALARM_CANCELLED, when=when)

        what = (
            "the next alarm to ring"
            if alarm is None
            else f"the alarm at {alarm.hour}:{alarm.minute:02d}"
        )
        return CoreResult(commands=(command,), audit=f"asked to cancel {what}", after=said)


class AlarmStatus(CoreSkill):
    source: ClassVar[str] = ALARM_STATUS_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        def said(results: Sequence[CommandResult]) -> str | None:
            """What the device holds, as it answered: one that does not say is a skill that failed,
            and not a guess."""
            state = answer_in(TimeState, results[0].data) if results else None
            if state is None:
                raise SkillRunError("the device did not say what alarms it has")
            alarms = state.alarms
            if not alarms:
                return core_say(language, CoreSay.ALARMS_NONE)
            if len(alarms) == 1:
                if alarms[0].ringing:
                    return core_say(language, CoreSay.ALARM_RINGING)
                when = say_when(language, alarms[0].at, state.now)
                return core_say(language, CoreSay.ALARM_NEXT, when=when)
            items = [
                core_say(language, CoreSay.ALARM_ITEM_RINGING)
                if alarm.ringing
                else say_when(language, alarm.at, state.now)
                for alarm in alarms
            ]
            return core_say(language, CoreSay.ALARMS_MANY, count=len(alarms), list=say_list(items))

        return CoreResult(
            commands=(GetState(),), audit="asked what alarms the device has", after=said
        )
