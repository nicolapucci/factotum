"""The values a model found in a message, checked against what the skill declares.

A model reads the parameters of a skill out of what was said, and small models do it loosely: a
number as text ("30"), an enum in another case ("On"), a value that is not one of the choices, a
key nobody declared. What is safe to run is decided here, in code: a value is accepted when it is of
the type the skill declares, inside its bounds, one of its choices; a small slip that leaves no
doubt is corrected, anything else is *invalid*, and a required value that is not there is *missing*.
Either way the person is asked again, and nothing runs on a guess.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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

    @property
    def ok(self) -> bool:
        return not self.missing and not self.invalid

    @property
    def asked_for(self) -> tuple[str, ...]:
        """What the person is to be asked for again, in the order the skill declares it."""
        return (*self.missing, *self.invalid)


def check_values(manifest: SkillManifest, given: Mapping[str, Any]) -> Checked:
    """Check what a model found against the parameters of the skill. Names the skill did not
    declare are dropped: they are not the person's."""
    return check_specs(manifest.parameters, manifest.required, given)


def check_specs(
    specs: Mapping[str, ParameterSpec], required: Sequence[str], given: Mapping[str, Any]
) -> Checked:
    """The same for any set of declared parameters (the ones of a device, for instance)."""
    values: dict[str, ParamValue] = {}
    invalid: list[str] = []
    for name, spec in specs.items():
        raw = given.get(name)
        if raw is None:
            continue
        accepted = _accept(spec, raw)
        if accepted is None:
            invalid.append(name)
        else:
            values[name] = accepted
    missing = [name for name in required if name not in values and name not in invalid]
    return Checked(values=values, missing=tuple(missing), invalid=tuple(invalid))


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
    if isinstance(value, int | float) and not isinstance(value, bool):
        if spec.minimum is not None and value < spec.minimum:
            return None
        if spec.maximum is not None and value > spec.maximum:
            return None
    return value


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
