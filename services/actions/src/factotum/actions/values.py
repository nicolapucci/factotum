"""The values a model found in a message, checked against what the skill declares.

A model reads the parameters of a skill out of what was said, and small models do it loosely: a
number as text ("30"), an enum in another case ("On"), a value that is not one of the choices, a
key nobody declared. What is safe to run is decided here, in code: a value is accepted when it is of
the type the skill declares, inside its bounds, one of its choices; a small slip that leaves no
doubt is corrected, anything else is *invalid*, and a required value that is not there is *missing*.
Either way the person is asked again, and nothing runs on a guess.

A value that has the right type and is inside its bounds may still be one that nobody said, and that
is the mistake of a small model (a level of 100 for "turn it up"). Given the words of the person,
the checks also ask that a number and a text can be found in them (``grounding``, ADR 0028): one
that cannot is *unsaid*, which is not a value at all, so an optional one is left out and a required
one is missing and the person is asked.

A parameter that declares ``formats`` (a time, a length of time, a day: ADR 0030) is another case: a
model writes the words, or a form of them, and what a skill is given is what code reads in the
message, written in the form the skill declared (``formats.convert``). What the person did not say,
and what cannot be written in any of the forms, is unsaid in the same way.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from factotum.actions.formats import convert
from factotum.actions.grounding import says
from factotum.contracts.skills import ParameterSpec, ParamType, ParamValue, SkillManifest

MAX_TEXT_CHARS = 500
"""The longest text value: what a skill is given as a parameter is a word or a phrase."""

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


@dataclass(frozen=True, slots=True)
class Checked:
    values: dict[str, ParamValue]
    """What is acceptable, as the skill declares it."""

    missing: tuple[str, ...] = ()
    """Required and not given."""

    invalid: tuple[str, ...] = ()
    """Given, and not acceptable."""

    unsaid: tuple[str, ...] = ()
    """Given, and acceptable, but not something that the person said: left out. If it was required
    it is also in ``missing``."""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.invalid

    @property
    def asked_for(self) -> tuple[str, ...]:
        """What the person is to be asked for again, in the order the skill declares it."""
        return (*self.missing, *self.invalid)


def check_values(manifest: SkillManifest, given: Mapping[str, Any], *, said: str) -> Checked:
    """Check what a model found against the parameters of the skill, and against the words of the
    person (``said``): a number or a text that they did not say is not a value. Names the skill
    did not declare are dropped: they are not the person's."""
    return check_specs(manifest.parameters, manifest.required, given, said=said)


def check_specs(
    specs: Mapping[str, ParameterSpec],
    required: Sequence[str],
    given: Mapping[str, Any],
    *,
    said: str | None = None,
) -> Checked:
    """The same for any set of declared parameters (the ones of a device, for instance, which no
    person says: they are not looked for in any words, and so ``said`` is not given)."""
    values: dict[str, ParamValue] = {}
    invalid: list[str] = []
    unsaid: list[str] = []
    for name, spec in specs.items():
        raw = given.get(name)
        if raw is None:
            continue
        if spec.formats and said is not None:
            written = convert(spec, raw, said)
            if written is None:
                unsaid.append(name)
            elif _inside_bounds(spec, written):
                values[name] = written
            else:
                invalid.append(name)
            continue
        accepted = _accept(spec, raw)
        if accepted is None:
            invalid.append(name)
        elif said is not None and not says(said, spec, accepted):
            unsaid.append(name)
        else:
            values[name] = accepted
    missing = [name for name in required if name not in values and name not in invalid]
    return Checked(
        values=values,
        missing=tuple(missing),
        invalid=tuple(invalid),
        unsaid=tuple(unsaid),
    )


def _accept(spec: ParameterSpec, raw: Any) -> ParamValue | None:
    """The value as the spec declares it, or nothing when it cannot be."""
    value = _as_type(spec.type, raw)
    if value is None:
        return None
    if isinstance(value, str):
        if spec.enum is not None:
            wanted = " ".join(value.casefold().split())
            for choice in spec.enum:
                if isinstance(choice, str) and " ".join(choice.casefold().split()) == wanted:
                    return choice
            return None
    elif spec.enum is not None and value not in spec.enum:
        return None
    return value if _inside_bounds(spec, value) else None


def _inside_bounds(spec: ParameterSpec, value: ParamValue) -> bool:
    if isinstance(value, int | float) and not isinstance(value, bool):
        if spec.minimum is not None and value < spec.minimum:
            return False
        if spec.maximum is not None and value > spec.maximum:
            return False
    return True


def _as_type(kind: ParamType, raw: Any) -> ParamValue | None:
    match kind:
        case ParamType.STRING:
            return _text(raw)
        case ParamType.BOOLEAN:
            return _boolean(raw)
        case ParamType.INTEGER:
            number = _number(raw)
            if number is None or not float(number).is_integer():
                return None
            return int(number)
        case ParamType.NUMBER:
            return _number(raw)


def _text(raw: Any) -> str | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    if not text or len(text) > MAX_TEXT_CHARS or not text.isprintable():
        return None
    return text


def _number(raw: Any) -> int | float | None:
    """A finite number, from a number or from text that is one ("30", "0.5", "30%")."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return raw if math.isfinite(raw) else None
    if isinstance(raw, str):
        text = raw.strip().removesuffix("%").strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _boolean(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().casefold()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None
