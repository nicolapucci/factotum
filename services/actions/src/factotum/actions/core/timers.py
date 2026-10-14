"""The skills of the platform for the timers of a device (ADR 0029): one is set, cancelled, and
asked how much time is left on.

Each is the interface between a model and the clock of a device. Toward the model it says what can
be asked: the *words* of how long (*five minutes*, *un'ora e mezza*), which the check of ADR 0028
finds in the message. Toward the device it passes on the seconds that code makes of them
(``when.parse_duration``: a model does no sums, and *300* is a number that was not said) and the
sound that rings, which the platform makes once, when the timer is set. The device keeps the time on
its own clock and rings by itself; what it answers is read here, in code, and said in a sentence of
``phrases`` in the language of the person. A model never reads it.
"""

from collections.abc import Sequence
from typing import ClassVar

from factotum.actions.core.phrases import CoreSay, core_say, say_duration, say_list
from factotum.actions.core.rings import ring_of
from factotum.actions.core.skills import CoreResult, CoreSkill, SpeechMaker
from factotum.actions.core.when import parse_duration
from factotum.contracts.commands import (
    MAX_TIMER_SECONDS,
    CancelTimer,
    CommandResult,
    GetState,
    SetTimer,
)
from factotum.contracts.errors import SkillFailedError, SkillRunError
from factotum.contracts.identity import Principal
from factotum.contracts.runner import RunRequest
from factotum.contracts.timekeeping import (
    TimersCancelled,
    TimerSet,
    TimeState,
    answer_in,
)

TIMER_SOURCE = """\
---
name: timer
description: >-
  Set a timer on the device that is being spoken to, for a length of time the person says: a
  countdown, or a reminder in some minutes (remind me in ten minutes). The device rings by itself
  when the time is up.
examples:
  - set a timer for five minutes
  - start a ten minute timer
  - set a timer for an hour and a half
  - timer for half an hour
  - metti un timer di cinque minuti
  - imposta un timer di mezz'ora
  - avvisami tra dieci minuti
target: system
parameters:
  duration:
    type: string
    description: >-
      how long the timer lasts, in the words the person said (five minutes, an hour and a half,
      cinque minuti)
required: [duration]
effect: reversible
timeout_s: 60
---
A skill of the platform: the interface between a model and the timers of a device. The words of
how long are read by code (a model does no sums), the voice that rings is made once by the model
gateway when the timer is set, and the device keeps the time on its own clock and rings by itself.
"""

TIMER_CANCEL_SOURCE = """\
---
name: timer-cancel
description: >-
  Cancel a timer on the device that is being spoken to, or stop one that is ringing: the one the
  person names by its length, or else the one that is ringing, or else the next one to ring. Use it
  whenever the person says timer, to cancel it, stop it or turn it off.
examples:
  - cancel the timer
  - stop the timer
  - cancel the five minute timer
  - turn off the timer
  - annulla il timer
  - ferma il timer
  - spegni il timer da cinque minuti
target: system
parameters:
  duration:
    type: string
    description: >-
      the length of the timer to cancel, in the words the person said, when they say which one
      (leave it out when they do not)
effect: reversible
---
A skill of the platform: the interface between a model and the timers of a device. It passes on
which timer the person named, if they did; which one it is when they did not is the device's to
choose (the one that rings, else the next to ring).
"""

TIMER_STATUS_SOURCE = """\
---
name: timer-status
description: >-
  Say how much time is left on the timers of the device that is being spoken to.
examples:
  - how much time is left on the timer
  - how long until the timer rings
  - are there any timers
  - quanto manca al timer
  - quanto tempo resta
  - ho dei timer attivi
target: system
effect: read_only
---
A skill of the platform: it asks the device how it is and says what the device answered, in the
language of the person. It changes nothing.
"""


def _length_of(words: object, language: str) -> int:
    """The seconds that the words of a person say, or the skill says why it did not understand."""
    seconds = parse_duration(words) if isinstance(words, str) else None
    if seconds is None:
        raise SkillFailedError(core_say(language, CoreSay.DURATION_UNCLEAR))
    if not 1 <= seconds <= MAX_TIMER_SECONDS:
        raise SkillFailedError(core_say(language, CoreSay.DURATION_RANGE))
    return seconds


class Timer(CoreSkill):
    source: ClassVar[str] = TIMER_SOURCE

    def __init__(self, speech: SpeechMaker) -> None:
        super().__init__()
        self._speech = speech

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        seconds = _length_of(request.params.get("duration"), language)
        ring = core_say(language, CoreSay.TIMER_RING, duration=say_duration(language, seconds))
        audio = await ring_of(self._speech, principal, ring)

        def said(results: Sequence[CommandResult]) -> str | None:
            """The timer that the device set, as it says it: a device that is full says so, and one
            that does not answer is a skill that failed."""
            answer = answer_in(TimerSet, results[0].data) if results else None
            if answer is None:
                raise SkillRunError("the device did not say which timer it set")
            if answer.timer is None:
                raise SkillFailedError(core_say(language, CoreSay.TIMERS_FULL))
            length = say_duration(language, answer.timer.seconds)
            return core_say(language, CoreSay.TIMER_SET, duration=length)

        return CoreResult(
            commands=(SetTimer(seconds=seconds, text=ring, audio=audio),),
            audit=f"timer set for {seconds} s",
            after=said,
        )


class TimerCancel(CoreSkill):
    source: ClassVar[str] = TIMER_CANCEL_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        named = request.params.get("duration")
        seconds = None if named is None else _length_of(named, language)

        def said(results: Sequence[CommandResult]) -> str | None:
            answer = answer_in(TimersCancelled, results[0].data) if results else None
            if answer is None:
                raise SkillRunError("the device did not say which timer it cancelled")
            if not answer.cancelled:
                if seconds is None:
                    return core_say(language, CoreSay.TIMER_NONE)
                length = say_duration(language, seconds)
                return core_say(language, CoreSay.TIMER_NONE_NAMED, duration=length)
            if len(answer.cancelled) > 1:
                return core_say(language, CoreSay.TIMERS_CANCELLED, count=len(answer.cancelled))
            length = say_duration(language, answer.cancelled[0].seconds)
            return core_say(language, CoreSay.TIMER_CANCELLED, duration=length)

        what = "the next timer to ring" if seconds is None else f"the timer of {seconds} s"
        return CoreResult(
            commands=(CancelTimer(seconds=seconds),),
            audit=f"asked to cancel {what}",
            after=said,
        )


class TimerStatus(CoreSkill):
    source: ClassVar[str] = TIMER_STATUS_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        def said(results: Sequence[CommandResult]) -> str | None:
            """What the device holds, as it answered: one that does not say is a skill that failed,
            and not a guess."""
            state = answer_in(TimeState, results[0].data) if results else None
            if state is None:
                raise SkillRunError("the device did not say what timers it has")
            timers = state.timers
            if not timers:
                return core_say(language, CoreSay.TIMERS_NONE)
            if len(timers) == 1:
                if timers[0].ringing:
                    return core_say(language, CoreSay.TIMER_RINGING)
                left = say_duration(language, timers[0].left_s)
                return core_say(language, CoreSay.TIMER_LEFT, left=left)
            items = [
                core_say(language, CoreSay.TIMER_ITEM_RINGING)
                if timer.ringing
                else core_say(
                    language, CoreSay.TIMER_ITEM_LEFT, left=say_duration(language, timer.left_s)
                )
                for timer in timers
            ]
            return core_say(language, CoreSay.TIMERS_MANY, count=len(timers), list=say_list(items))

        return CoreResult(
            commands=(GetState(),), audit="asked what timers the device has", after=said
        )
