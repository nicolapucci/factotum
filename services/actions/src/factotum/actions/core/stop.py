"""The skill of the platform that stops what is ringing on a device (ADR 0029).

*Enough*, *stop*, *basta* are said to whatever is making the noise, and a person who says them does
not say whether it is a timer or an alarm: so this skill names nothing, and the device, which knows
what rings, stops all of it. It never cancels a timer or an alarm that has not rung (that is what
``timer-cancel`` and ``alarm-cancel`` are for): a *stop* with nothing ringing is told so.

It is the interface between a model and the device, as the others are: the answer of the device
(what it stopped) is read here, in code, and said in a sentence of ``phrases``.
"""

from collections.abc import Sequence
from typing import ClassVar

from factotum.actions.core.phrases import CoreSay, core_say
from factotum.actions.core.skills import CoreResult, CoreSkill
from factotum.contracts.commands import CommandResult, StopRinging
from factotum.contracts.errors import SkillRunError
from factotum.contracts.identity import Principal
from factotum.contracts.runner import RunRequest
from factotum.contracts.timekeeping import RingsStopped, answer_in

STOP_SOURCE = """\
---
name: stop
description: >-
  Stop whatever is ringing on the device that is being spoken to, a timer or an alarm, when the
  person only wants the noise to stop and does not say which it is: enough, stop, be quiet, basta,
  zitto. If the person says timer or alarm it is not this skill. It does not mute the device and
  it cancels none that has not rung.
examples:
  - stop
  - enough
  - that is enough
  - be quiet
  - silence
  - basta
  - basta così
  - ferma
  - zitto
  - silenzio
target: system
effect: reversible
---
A skill of the platform: the interface between a model and what a device rings with. It names
nothing: which timer or alarm is ringing is the device's to know, and the device stops all of them.
"""


class Stop(CoreSkill):
    source: ClassVar[str] = STOP_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        def said(results: Sequence[CommandResult]) -> str | None:
            """What the device stopped, as it says it: nothing is a thing to say, and a device that
            does not answer is a skill that failed."""
            answer = answer_in(RingsStopped, results[0].data) if results else None
            if answer is None:
                raise SkillRunError("the device did not say what it stopped")
            if not answer.timers and not answer.alarms:
                return core_say(language, CoreSay.NOTHING_RINGS)
            return core_say(language, CoreSay.RINGS_STOPPED)

        return CoreResult(commands=(StopRinging(),), audit="asked to stop what rings", after=said)
