"""The skills of the platform for the alarms of a device (ADR 0029): one is set, put off, moved,
cancelled, and asked about.

Each is the interface between a model and the clock of a device, and the device has the clock and
the calendar of the place it is in: the skill says only *set the alarm for this hour on this day*,
*put it off by this long* or *move it to this hour*.
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
from factotum.actions.core.when import AlarmWords, parse_alarm, parse_duration
from factotum.contracts.commands import (
    MAX_TIMER_SECONDS,
    AlarmDay,
    CancelAlarm,
    CommandResult,
    GetState,
    MoveAlarm,
    PostponeAlarm,
    SetAlarm,
)
from factotum.contracts.errors import SkillFailedError, SkillRunError
from factotum.contracts.identity import Principal
from factotum.contracts.runner import RunRequest
from factotum.contracts.timekeeping import (
    AlarmsCancelled,
    AlarmSet,
    AlarmsMoved,
    AlarmsPostponed,
    TimeState,
    answer_in,
)

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
  the hour the person names, or else the one that is ringing, or else the next one to ring. Use it
  whenever the person says alarm (sveglia), to cancel it, stop it or turn it off.
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

ALARM_POSTPONE_SOURCE = """\
---
name: alarm-postpone
description: >-
  Put an alarm off on the device that is being spoken to, or snooze one that is ringing, by as
  long as the person says: it rings again by itself later (snooze, rimanda la sveglia, five more
  minutes, ancora cinque minuti). Only for an alarm that is set or ringing, never for a new
  countdown. When they say no length, the device puts it off by its own snooze.
examples:
  - snooze the alarm
  - put the alarm off by ten minutes
  - five more minutes
  - postpone the alarm by half an hour
  - rimanda la sveglia
  - rimanda la sveglia di dieci minuti
  - ancora cinque minuti
  - posticipa la sveglia di mezz'ora
target: system
parameters:
  by:
    type: string
    description: >-
      how much later, in the words the person said (ten minutes, half an hour, dieci minuti), when
      they say it (leave it out when they do not: the device has its own snooze)
effect: reversible
---
A skill of the platform: the interface between a model and the alarms of a device. It passes on how
much later the person said, if they did. How long a snooze is when they did not say, and which
alarm it is (the one that is ringing, else the next to ring), are the device's to choose.
"""

ALARM_CHANGE_SOURCE = """\
---
name: alarm-change
description: >-
  Change the hour of an alarm on the device that is being spoken to: move it to the hour the person
  says, on the day they say if they say one. The alarm that is moved is the one the person names, or
  else the one that is ringing, or else the next one to ring.
examples:
  - move the alarm to eight
  - change the alarm to seven thirty
  - move the seven o'clock alarm to eight
  - sposta la sveglia alle otto
  - cambia la sveglia alle sette e mezza
  - sposta la sveglia delle sette alle otto
  - sposta la sveglia a domani alle sei
target: system
parameters:
  time:
    type: string
    description: >-
      the hour the alarm is moved to, in the words the person said (eight, 7:30 pm, alle otto)
  day:
    type: string
    description: >-
      the day it is moved to, in the words the person said (tomorrow, monday, domani), when they say
      one (leave it out when they do not)
  from:
    type: string
    description: >-
      the hour the alarm to move has now, in the words the person said, only when they name that
      alarm (it is not the hour it is moved to; leave it out when they do not name it)
required: [time]
effect: reversible
---
A skill of the platform: the interface between a model and the alarms of a device. The words of the
hour and of the day are read by code, as for a new alarm, and the device, which has the clock and
the calendar of the place it is in, works out the next time that fits and moves the alarm to it; if
that moment has gone by, or the date never comes, the alarm stays where it was.
"""

ALARM_STATUS_SOURCE = """\
---
name: alarm-status
description: >-
  Say what alarms are set on the device that is being spoken to, and when the next one rings (when
  is my alarm, quali sveglie ho).
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


def _postponement_of(words: object, language: str) -> int:
    """The seconds that the words of a person say an alarm is put off by, or the skill says why it
    did not understand."""
    seconds = parse_duration(words) if isinstance(words, str) else None
    if seconds is None:
        raise SkillFailedError(core_say(language, CoreSay.DURATION_UNCLEAR))
    if not 1 <= seconds <= MAX_TIMER_SECONDS:
        raise SkillFailedError(core_say(language, CoreSay.POSTPONE_RANGE))
    return seconds


class AlarmPostpone(CoreSkill):
    source: ClassVar[str] = ALARM_POSTPONE_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        by = request.params.get("by")
        seconds = None if by is None else _postponement_of(by, language)

        def said(results: Sequence[CommandResult]) -> str | None:
            """The alarm as the device has it now. There being none to put off is a thing to say,
            as it is for a cancel; a device that does not answer is a skill that failed."""
            answer = answer_in(AlarmsPostponed, results[0].data) if results else None
            if answer is None:
                raise SkillRunError("the device did not say which alarm it put off")
            if not answer.postponed:
                return core_say(language, CoreSay.ALARM_NONE_TO_CHANGE)
            if len(answer.postponed) > 1:
                return core_say(language, CoreSay.ALARMS_POSTPONED, count=len(answer.postponed))
            when = say_when(language, answer.postponed[0].at, answer.now)
            return core_say(language, CoreSay.ALARM_WILL_RING, when=when)

        by_how_much = "the snooze of the device" if seconds is None else f"{seconds} s"
        return CoreResult(
            commands=(PostponeAlarm(seconds=seconds),),
            audit=f"asked to put the alarm off by {by_how_much}",
            after=said,
        )


class AlarmChange(CoreSkill):
    source: ClassVar[str] = ALARM_CHANGE_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        hour = request.params.get("time")
        day = request.params.get("day")
        named = request.params.get("from")
        if not isinstance(hour, str) or not (day is None or isinstance(day, str)):
            raise SkillFailedError(core_say(language, CoreSay.TIME_UNCLEAR))
        alarm = _alarm_of(f"{day or ''} {hour}", language)
        was = None if named is None else _alarm_of(str(named), language)
        command = MoveAlarm(
            hour=alarm.hour,
            minute=alarm.minute,
            day=alarm.day,
            from_hour=None if was is None else was.hour,
            from_minute=None if was is None else was.minute,
        )

        def said(results: Sequence[CommandResult]) -> str | None:
            """Where the alarm is now. There being none to move is a thing to say (as for a cancel);
            a moment that has gone by and a date that never comes are answers of the device that
            the skill fails with, in its words, as it does for an alarm that is set."""
            answer = answer_in(AlarmsMoved, results[0].data) if results else None
            if answer is None:
                raise SkillRunError("the device did not say which alarm it moved")
            if answer.reason == "none":
                if was is None:
                    return core_say(language, CoreSay.ALARM_NONE_TO_CHANGE)
                at = say_clock(language, was.hour, was.minute)
                return core_say(language, CoreSay.ALARM_NONE_NAMED, at=at)
            if answer.reason is not None:
                reasons = {"past": CoreSay.ALARM_PAST, "no_such_day": CoreSay.ALARM_NO_SUCH_DAY}
                raise SkillFailedError(core_say(language, reasons[answer.reason]))
            if len(answer.moved) > 1:
                return core_say(language, CoreSay.ALARMS_MOVED, count=len(answer.moved))
            when = say_when(language, answer.moved[0].at, answer.now)
            return core_say(language, CoreSay.ALARM_WILL_RING, when=when)

        which = (
            "the next alarm to ring" if was is None else f"the alarm at {was.hour}:{was.minute:02d}"
        )
        return CoreResult(
            commands=(command,),
            audit=(
                f"asked to move {which} to {alarm.hour:02d}:{alarm.minute:02d} "
                f"({_said_day(alarm.day)})"
            ),
            after=said,
        )


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
