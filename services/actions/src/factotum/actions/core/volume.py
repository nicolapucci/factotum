"""The skills of the platform for the sound of a device: its volume, turned up or down or set to a
level, its mute, and how it is (ADR 0029).

Each is the interface between a model and the speaker of a device. Toward the model it says what
can be asked (a manifest, whose values are checked in code: ADR 0028); toward the device it passes
on exactly what was said as a command of the vocabulary, and tells it no number of its own. How far
"turn it up" goes when nobody says by how much is the step of the device. They are code of the
platform, run in the process of the actions service: no sandbox and no runner, so they really do it.

What the device answers (the level the volume came to; how it is) is read here, in code, and said in
a sentence of ``phrases`` in the language of the person. A model never reads it.
"""

from collections.abc import Sequence
from typing import ClassVar

from factotum.actions.core.phrases import CoreSay, core_say
from factotum.actions.core.skills import CoreResult, CoreSkill
from factotum.contracts.commands import (
    AdjustVolume,
    CommandResult,
    GetState,
    SetMute,
    SetVolume,
    VolumeDirection,
)
from factotum.contracts.errors import SkillRunError
from factotum.contracts.identity import Principal
from factotum.contracts.runner import RunRequest

VOLUME_SOURCE = """\
---
name: volume
description: Set the volume of the device that is being spoken to, to a level the person says.
examples:
  - set the volume to 30
  - volume at fifty percent
  - alza il volume a 70
  - abbassa il volume a 20
target: system
parameters:
  level: {type: integer, minimum: 0, maximum: 100, description: "the volume, in percent"}
required: [level]
effect: reversible
---
A skill of the platform: the interface between a model and the speaker of a device. It gives the
device the level that was said and nothing else. To turn the volume up or down without a level, see
`volume-change`: a manifest cannot say that one of two values is required, so they are two skills.
"""

VOLUME_CHANGE_SOURCE = """\
---
name: volume-change
description: >-
  Turn the volume of the device that is being spoken to up or down, by an amount if the person
  says one.
examples:
  - turn the volume up
  - turn it down a bit
  - make it louder
  - turn the volume down by 20
  - alza il volume
  - abbassa il volume di 20
  - più forte
target: system
parameters:
  direction:
    type: string
    enum: ["up", "down"]
    description: whether the volume is to go up or to go down
  amount:
    type: integer
    minimum: 1
    maximum: 100
    description: by how much, in percent, when the person says it (leave it out when they do not)
required: [direction]
effect: reversible
---
A skill of the platform: the interface between a model and the speaker of a device. Toward the model
it says what can be asked (a direction, and by how much if the person said it); toward the device it
passes exactly that on. A person who does not say by how much ("alza il volume") gets the step of
the device, which is the device's to know (a speaker and a television do not turn it by the same
amount): the skill tells the device no number of its own.
"""

MUTE_SOURCE = """\
---
name: mute
description: Mute the sound of the device that is being spoken to, or bring it back.
examples:
  - mute
  - mute the sound
  - unmute
  - turn the sound back on
  - metti in muto
  - togli il muto
  - silenzia il dispositivo
target: system
parameters:
  muted: {type: boolean, description: "true to mute the sound, false to bring it back"}
required: [muted]
effect: reversible
---
A skill of the platform: the interface between a model and the speaker of a device. The device keeps
the level that the volume was at, so that bringing the sound back brings back what it was.
"""

VOLUME_STATUS_SOURCE = """\
---
name: volume-status
description: >-
  Say how loud the device that is being spoken to is, and whether its sound is muted.
examples:
  - what is the volume
  - how loud is it
  - is it muted
  - che volume c'è
  - a che volume sei
  - sei in muto
target: system
effect: read_only
---
A skill of the platform: it asks the device how it is and says what the device answered, in the
language of the person. It changes nothing.
"""


def _level(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class Volume(CoreSkill):
    source: ClassVar[str] = VOLUME_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        level = _level(request.params.get("level"))
        if level is None or not 0 <= level <= 100:
            raise SkillRunError("the volume is a level from 0 to 100")
        return CoreResult(
            message=core_say(language, CoreSay.VOLUME_AT, level=level),
            commands=(SetVolume(level=level),),
            audit=f"volume set to {level}",
        )


class VolumeChange(CoreSkill):
    source: ClassVar[str] = VOLUME_CHANGE_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        try:
            direction = VolumeDirection(str(request.params.get("direction")))
        except ValueError:
            raise SkillRunError("the volume goes up or down") from None
        raw = request.params.get("amount")
        amount = None if raw is None else _level(raw)
        if raw is not None and (amount is None or not 1 <= amount <= 100):
            raise SkillRunError("the volume is turned by 1 to 100 points")

        def said(results: Sequence[CommandResult]) -> str | None:
            """Where the volume came to, if the device says."""
            level = _level((results[0].data or {}).get("volume")) if results else None
            return None if level is None else core_say(language, CoreSay.VOLUME_AT, level=level)

        how_much = "by its own step" if amount is None else f"by {amount}"
        return CoreResult(
            commands=(AdjustVolume(direction=direction, amount=amount),),
            audit=f"volume turned {direction.value} {how_much}",
            after=said,
        )


class Mute(CoreSkill):
    source: ClassVar[str] = MUTE_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        muted = request.params.get("muted")
        if not isinstance(muted, bool):
            raise SkillRunError("the sound is muted or it is not")
        return CoreResult(
            message=core_say(language, CoreSay.MUTED if muted else CoreSay.UNMUTED),
            commands=(SetMute(muted=muted),),
            audit="sound muted" if muted else "sound brought back",
        )


class VolumeStatus(CoreSkill):
    source: ClassVar[str] = VOLUME_STATUS_SOURCE

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        def said(results: Sequence[CommandResult]) -> str | None:
            """How the device is, as it answered: a device that does not say is a skill that
            failed, and not a guess."""
            answer = results[0].data or {} if results else {}
            level, muted = _level(answer.get("volume")), answer.get("muted")
            if level is None or not isinstance(muted, bool):
                raise SkillRunError("the device did not say how loud it is")
            key = CoreSay.SOUND_MUTED if muted else CoreSay.SOUND_AT
            return core_say(language, key, level=level)

        return CoreResult(commands=(GetState(),), audit="asked how loud the device is", after=said)
