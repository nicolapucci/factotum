"""What a plan comes to, once it has been checked.

The model proposes; this decides. The skill has to be one of those that were offered, the device one
of those the caller may use and one the skill is set up for, and the values what the skill
declares. Anything else does not become an action: a skill the model made up is *nothing to do*,
a device it could not name or a value that is not there is *needs more*, and the person is asked. A
plan that passes is an ``Understood``: the only thing that goes on to be authorized.

This is a pure function of the plan and of what the caller may use, so every rule is tested.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from factotum.actions.directory import BoundDevice, named
from factotum.actions.matching import Candidate
from factotum.actions.phrases import Say, language_of, say
from factotum.actions.planner import Plan
from factotum.actions.values import check_specs, check_values
from factotum.common.language import detect_language
from factotum.contracts.actions import ActCode
from factotum.contracts.devices import ExternalDeviceRecord, SystemDeviceRecord
from factotum.contracts.skills import ParamValue, SkillTarget

MAX_SENTENCE_CHARS = 300


@dataclass(frozen=True, slots=True)
class Understood:
    """A skill was chosen, and everything it needs is known."""

    candidate: Candidate
    values: dict[str, ParamValue]
    device_params: dict[str, ParamValue]
    target: SystemDeviceRecord | ExternalDeviceRecord | None
    language: str
    question: str | None
    """What to ask before doing it, in the language of the message, if the model wrote one."""

    left_out: tuple[str, ...] = ()
    """The values the model wrote and the person did not say (ADR 0028): they are not values, and
    the skill is called without them."""


@dataclass(frozen=True, slots=True)
class NeedsMore:
    """A skill was chosen and something is missing or unclear: the reply asks for it."""

    code: ActCode
    skill: str
    reply: str
    left_out: tuple[str, ...] = ()
    """The values the model wrote and the person did not say: asked for again when they are
    required."""


@dataclass(frozen=True, slots=True)
class NothingToDo:
    """The message asks for none of the skills."""


Understanding = Understood | NeedsMore | NothingToDo


def clean_sentence(text: str | None) -> str | None:
    """A sentence a model wrote, to be said as it is: one line, not too long, printable. Anything
    else is not said."""
    if text is None:
        return None
    sentence = " ".join(text.split())
    if len(sentence) < 3 or len(sentence) > MAX_SENTENCE_CHARS or not sentence.isprintable():
        return None
    return sentence


def understand(
    plan: Plan,
    candidates: Sequence[Candidate],
    *,
    message: str,
    here: SystemDeviceRecord | None,
    systems: Sequence[SystemDeviceRecord],
    bound: Mapping[str, Sequence[BoundDevice]],
) -> Understanding:
    """What ``plan`` asks for, checked against what was offered, what the caller may use and what
    ``message`` says (a value that the person did not say is not one: ``grounding``).

    ``here`` is the device that was spoken to, ``systems`` the system devices the caller may use,
    ``bound`` the external devices each skill is set up for that the caller may use."""
    if not plan.skill:
        return NothingToDo()
    wanted = plan.skill.strip().casefold()
    chosen = next((c for c in candidates if c.skill.name.casefold() == wanted), None)
    if chosen is None or chosen.skill.enabled is None:
        return NothingToDo()
    manifest = chosen.skill.enabled.manifest
    language = language_of(plan.language)

    target: SystemDeviceRecord | ExternalDeviceRecord | None = None
    device_params: dict[str, ParamValue] = {}
    if manifest.target is SkillTarget.SYSTEM:
        picked = _system_target(plan.device, here, systems)
        if isinstance(picked, _Unresolved):
            return _needs_more(picked, manifest.name, language)
        target = picked
    elif manifest.target is SkillTarget.EXTERNAL:
        options = bound.get(manifest.name, [])
        found = _external_target(plan.device, options)
        if isinstance(found, _Unresolved):
            return _needs_more(found, manifest.name, language)
        target = found.device
        checked = check_specs(
            manifest.device_parameters, tuple(manifest.device_parameters), found.params
        )
        if not checked.ok:
            # The skill is set up for this device with what the skill does not accept: it is
            # not something a person can be asked for, and nothing is run on it.
            return NeedsMore(ActCode.NO_DEVICE, manifest.name, say(language, Say.NO_DEVICE))
        device_params = checked.values

    values = check_values(manifest, plan.parameters, said=message)
    if not values.ok:
        return NeedsMore(
            ActCode.MISSING_VALUES,
            manifest.name,
            _what_is_asked(plan, message, language, values.asked_for),
            left_out=values.unsaid,
        )
    return Understood(
        candidate=chosen,
        values=values.values,
        device_params=device_params,
        target=target,
        language=language,
        question=clean_sentence(plan.question),
        left_out=values.unsaid,
    )


def _what_is_asked(plan: Plan, message: str, language: str, names: Sequence[str]) -> str:
    """What the person is asked for a value that is missing: the question that the model wrote, if
    it is a sentence and it is in the language of the message, and the code's own words otherwise
    (a model that was shown a question in English asks in English whatever it is spoken to in)."""
    spoken = detect_language(message)
    ask = clean_sentence(plan.ask)
    if ask is not None:
        asked_in = detect_language(ask)
        # The same language, or one of the two cannot be told (a name, a word): the model's own.
        if spoken is None or asked_in is None or asked_in == spoken:
            return ask
    return say(spoken or language, Say.NEEDS_VALUES, names=", ".join(names))


@dataclass(frozen=True, slots=True)
class _Unresolved:
    code: ActCode
    names: tuple[str, ...] = ()


def _needs_more(unresolved: _Unresolved, skill: str, language: str) -> NeedsMore:
    if unresolved.code is ActCode.AMBIGUOUS_DEVICE:
        reply = say(language, Say.WHICH_DEVICE, names=", ".join(unresolved.names))
    else:
        reply = say(language, Say.NO_DEVICE)
    return NeedsMore(unresolved.code, skill, reply)


def _system_target(
    said: str | None, here: SystemDeviceRecord | None, systems: Sequence[SystemDeviceRecord]
) -> SystemDeviceRecord | _Unresolved:
    if said:
        found = named(systems, said)
        if len(found) == 1:
            return found[0]
        return _Unresolved(ActCode.AMBIGUOUS_DEVICE, tuple(d.name for d in systems))
    if here is not None:
        return here
    return _Unresolved(ActCode.NO_DEVICE)


def _external_target(said: str | None, options: Sequence[BoundDevice]) -> BoundDevice | _Unresolved:
    if not options:
        return _Unresolved(ActCode.NO_DEVICE)
    if said:
        matches = [b for b in options if named([b.device], said)]
        if len(matches) == 1:
            return matches[0]
        return _Unresolved(ActCode.AMBIGUOUS_DEVICE, tuple(b.device.name for b in options))
    if len(options) == 1:
        return options[0]
    return _Unresolved(ActCode.AMBIGUOUS_DEVICE, tuple(b.device.name for b in options))
