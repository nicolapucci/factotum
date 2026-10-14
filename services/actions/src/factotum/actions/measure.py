"""Measuring a planner against the corpus (``plan_cases``): what became of a message, and what is
wrong with it.

The corpus says what a model that reads each message well does with it. What a model did is what
the service made of its reading: the outcome, the skill that was chosen and the values that skill
was called with. Those are in the audit trail whatever kind of skill it is (a script that the
runner ran, a skill of the platform that is code in the service, a plan that was written), so that
is where they are read from; the runner is looked at only for the device a script was bound to, and
the clips that a device was given are for an announcement, which is judged by the words that were
said.

The live test (a real model) and a unit test (a planner that is told the right answer) both measure
with this, so that a harness that cannot give a perfect score to a perfect planner is found by a
test and not by a run of minutes: it counted the skills of the platform as "nothing was run" for as
long as only the runner was looked at.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from factotum.actions.core.when import parse_alarm, parse_duration
from factotum.actions.plan_cases import PlanCase
from factotum.actions.rig import ActionsRig
from factotum.contracts.actions import ActOutcome, ActStatus
from factotum.contracts.commands import PlayAudio
from factotum.contracts.identity import Principal
from factotum.contracts.skills import ParamValue

ENTITIES = {
    "Living room lamp": "light.living_room",
    "Bedroom lamp": "light.bedroom",
    "Garage door": "cover.garage",
}
"""The entity each external device of the example home is bound to."""


@dataclass
class Judged:
    case: PlanCase
    outcome: ActOutcome
    seconds: float
    values: Mapping[str, ParamValue] | None
    """What the skill was called with, or nothing when it was not called."""

    problems: list[str]

    @property
    def right(self) -> bool:
        return not self.problems


def same(understood: object, wanted: ParamValue) -> bool:
    """What a person said is not what a model has to repeat letter for letter: a text is the same
    whatever its case and whatever punctuation it ends with."""
    if isinstance(understood, str) and isinstance(wanted, str):
        return understood.strip().strip(".!?").casefold() == wanted.strip().strip(".!?").casefold()
    return understood == wanted


def _length(words: str) -> object:
    return parse_duration(words)


def _hour(words: str) -> object:
    alarm = parse_alarm(words)
    return None if alarm is None else (alarm.hour, alarm.minute)


def _day(words: str) -> object:
    alarm = parse_alarm(f"{words} 12:00")
    return None if alarm is None else alarm.day


_READ_AS: dict[tuple[str, str], Callable[[str], object]] = {
    ("timer", "duration"): _length,
    ("timer-cancel", "duration"): _length,
    ("alarm-postpone", "by"): _length,
    ("alarm", "time"): _hour,
    ("alarm-cancel", "time"): _hour,
    ("alarm-change", "time"): _hour,
    ("alarm-change", "from"): _hour,
    ("alarm", "day"): _day,
    ("alarm-change", "day"): _day,
}
"""The values that code reads (a length, an hour, a day): what a model wrote for them is judged by
what the code makes of it and not by its letters, since the code reads *alle otto* and *otto* as
the same hour (and *ten minutes* and *ten minute* as the same length)."""


_INTRODUCING = frozenset({"to", "di", "that", "che"})
"""The word that begins what is to be remembered or said (*remind me to call*, *ricordami di
chiamare*, *tell her that*): a model that copies it has copied the words that were said."""


def _with_the_word_that_introduces_it(understood: object, wanted: ParamValue) -> bool:
    if not isinstance(understood, str) or not isinstance(wanted, str):
        return False
    first, _, rest = understood.strip().partition(" ")
    return first.casefold() in _INTRODUCING and same(rest, wanted)


def same_value(skill: str | None, name: str, understood: object, wanted: ParamValue) -> bool:
    """Whether what a model wrote for a value of a skill is what the case wants: the same words
    (with the word that introduces them, if it copied that), or, for a value that code reads, the
    same reading."""
    reader = _READ_AS.get((skill or "", name))
    if reader is not None and isinstance(understood, str) and isinstance(wanted, str):
        read = reader(understood)
        if read is not None and read == reader(wanted):
            return True
    return same(understood, wanted) or _with_the_word_that_introduces_it(understood, wanted)


def judge(
    case: PlanCase,
    outcome: ActOutcome,
    values: Mapping[str, ParamValue] | None,
    device_params: Mapping[str, Any] | None = None,
    said: Sequence[str] = (),
) -> list[str]:
    """What is wrong with what became of a message, in a few words each; nothing when it is
    right. ``values`` are those the skill was called with and ``device_params`` those of the
    external device it was bound to, when a script ran. ``said`` is the words of the clips that a
    device was given: an announcement is judged by what was said."""
    problems: list[str] = []
    if outcome.status is not case.status:
        problems.append(f"{outcome.status.value} instead of {case.status.value}")
    if outcome.skill != case.skill:
        problems.append(f"skill {outcome.skill} instead of {case.skill}")
    if case.skill == "announce":
        if case.status is ActStatus.DONE:
            if not said:
                problems.append("nothing was said")
            elif not same(" ".join(said), case.params["message"]):
                problems.append(
                    f"{' '.join(said)!r} was said instead of {case.params['message']!r}"
                )
    elif values is not None:
        for name, value in case.params.items():
            if not same_value(case.skill, name, values.get(name), value):
                problems.append(f"{name}={values.get(name)!r} instead of {value!r}")
        if outcome.skill == case.skill:  # a value that was run and that nobody said (ADR 0028)
            problems.extend(
                f"{name}={value!r} was run and nobody said it"
                for name, value in values.items()
                if name not in case.params
            )
        if (
            device_params is not None
            and case.device in ENTITIES
            and device_params.get("entity_id") != ENTITIES[case.device]
        ):
            problems.append(f"device {device_params.get('entity_id')} instead of {case.device}")
    elif case.status is ActStatus.DONE:
        problems.append("nothing was run")
    return problems


async def forget_what_is_asked(rig: ActionsRig, principal: Principal) -> None:
    """A yes or a no that was asked for is answered by the next message on that device: what is
    measured is one message at a time."""
    pending = await rig.store.get_pending(rig.tenant, principal.acts_as, principal.device_id)
    if pending is not None:
        await rig.store.take_pending(rig.tenant, pending.id)


async def measure(rig: ActionsRig, case: PlanCase) -> Judged:
    """Say a message of the corpus to the device it is for, and judge what became of it."""
    principal = rig.principal(case.speaker)
    await forget_what_is_asked(rig, principal)
    runs = len(rig.runner.requests)
    given = len(rig.dispatcher.given)
    trail = await rig.audit()
    last = trail[-1].id if trail else None
    began = time.perf_counter()
    outcome = await rig.hear(case.speaker, case.message)
    seconds = time.perf_counter() - began
    ran = rig.runner.requests[-1] if len(rig.runner.requests) > runs else None
    trail = await rig.audit()
    record = trail[-1] if trail and trail[-1].id != last else None
    said = [c.text for _, _, c in rig.dispatcher.given[given:] if isinstance(c, PlayAudio)]
    values = None if record is None else record.params
    device_params = ran.device_params if ran is not None else None
    problems = judge(case, outcome, values, device_params, said)
    return Judged(case, outcome, seconds, values, problems)
