"""The skill of the platform that says what time it is, and what day, by the clock of a device (ADR
0029).

The platform has no clock, and a model has none either (ADR 0028): the only one that knows what
time it is where a device stands is the device, so the question is asked of the device
(``get_state``, as the volume and the timers are) and what its wall shows is said here, in code, in
a sentence of ``phrases`` in the language of the person. A model never says the time and never
reads it.

It is the interface between a model and the clock of the device, as the others are: toward the model
it says what can be asked (the time, the date, or both); toward the device it asks how it is and
changes nothing. It tells the time of the place the device is in and no other, and the date of today
and no other day.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar

from factotum.actions.core.phrases import date_sentence, time_sentence
from factotum.actions.core.skills import CoreResult, CoreSkill
from factotum.contracts.commands import CommandResult, GetState
from factotum.contracts.errors import SkillRunError
from factotum.contracts.identity import Principal
from factotum.contracts.runner import RunRequest
from factotum.contracts.timekeeping import TimeState, answer_in

CLOCK_SOURCE = """\
---
name: clock
description: >-
  Say what time it is, or what day it is today, by the clock of the device that is being spoken
  to. It cannot tell the time in another place or the day of another date.
examples:
  - what time is it
  - what is the time
  - tell me the time
  - what day is it today
  - what is today's date
  - che ore sono
  - che ora è
  - dimmi l'ora
  - che giorno è oggi
  - che data è oggi
target: system
parameters:
  about:
    type: string
    enum: ["time", "date", "both"]
    description: >-
      what the person asks for: the time, the date (what day it is), or both (leave it out when they
      do not say)
effect: read_only
---
A skill of the platform: it asks the device what its wall shows and says it, in the language of the
person. Nobody but the device knows what time it is where it stands, so the platform never says it
from a clock of its own. A person who asks for both, or for neither in particular, is told both. It
changes nothing.
"""


class About(StrEnum):
    """What a person asks of the clock."""

    TIME = "time"
    DATE = "date"
    BOTH = "both"


class Clock(CoreSkill):
    source: ClassVar[str] = CLOCK_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        raw = request.params.get("about")
        try:
            about = About.BOTH if raw is None else About(str(raw))
        except ValueError:
            raise SkillRunError("the clock says the time, the date or both") from None

        def said(results: Sequence[CommandResult]) -> str | None:
            """What the wall of the device shows, as it answered: a device that does not say is a
            skill that failed, and not a guess."""
            state = answer_in(TimeState, results[0].data) if results else None
            if state is None:
                raise SkillRunError("the device did not say what time it is")
            sentences = []
            if about is not About.DATE:
                sentences.append(time_sentence(language, state.now))
            if about is not About.TIME:
                sentences.append(date_sentence(language, state.now))
            return " ".join(sentences)

        asked = {
            About.BOTH: "what time and day it is",
            About.TIME: "what time it is",
            About.DATE: "what day it is",
        }[about]
        return CoreResult(commands=(GetState(),), audit=f"asked the device {asked}", after=said)
