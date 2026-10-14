"""The formats in which a skill takes a value that a person says in words (ADR 0030).

A person says *sette e mezza*, *tra dieci minuti*, *domani*, *il 16 ottobre*; a skill wants
``07:30``, ``600``, ``+1d`` or ``16/10/26``. The skill says in its manifest which form it takes,
and the system turns what was said into it (``factotum.actions.formats``): the model is asked for
the words that were said, and code does the sums. This module is the declaration: the kinds of value
that words give, the patterns that write them, and the check that a pattern means what it says and
fits the type of the parameter it is declared for.

A parameter declares one or more formats, all of one *group* (the same words are read the same way):

* ``time`` writes a time of day with the tokens ``HH`` and ``H`` (24-hour clock), ``hh`` and ``h``
  (12-hour clock, with ``a`` for am or pm) and ``mm`` (``HH:mm`` is ``07:30``);
* ``duration`` is a length of time in ``seconds``, ``minutes`` or ``hours`` (a number) or ``iso``
  (``PT1H30M``, text);
* ``date`` writes the day of a month with ``dd`` and ``d``, the month with ``MM`` and ``M`` and, if
  there is one, the year with ``yyyy`` or ``yy`` (``dd/MM/yy`` is ``16/10/26``);
* ``in_days`` writes how many days from today with ``{n}`` (``+{n}d`` is ``+1d``, and ``{n}`` alone
  is the whole number);
* ``weekday`` writes the day of the week as ``name`` (``monday``) or ``iso`` (1 for Monday).

Nothing here reads words or knows today's date: the platform has no clock, so a day that is said as
*tomorrow* can only be written in a form that counts from today (``in_days``).
"""

from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field

from factotum.contracts.base import Contract

MAX_PATTERN_CHARS = 40


class FormatKind(StrEnum):
    TIME = "time"
    DURATION = "duration"
    DATE = "date"
    IN_DAYS = "in_days"
    WEEKDAY = "weekday"


class FormatGroup(StrEnum):
    """What the words are read as: formats of a group are ways of writing the same reading."""

    TIME = "time"
    DURATION = "duration"
    DAY = "day"


GROUP_OF: dict[FormatKind, FormatGroup] = {
    FormatKind.TIME: FormatGroup.TIME,
    FormatKind.DURATION: FormatGroup.DURATION,
    FormatKind.DATE: FormatGroup.DAY,
    FormatKind.IN_DAYS: FormatGroup.DAY,
    FormatKind.WEEKDAY: FormatGroup.DAY,
}

TIME_TOKENS = ("HH", "H", "hh", "h", "mm", "a")
HOURS_OF_THE_24_HOUR_CLOCK = ("HH", "H")
HOURS_OF_THE_12_HOUR_CLOCK = ("hh", "h")
DATE_TOKENS = ("dd", "d", "MM", "M", "yyyy", "yy")
DAYS_OF_THE_MONTH = ("dd", "d")
MONTHS = ("MM", "M")
YEARS = ("yyyy", "yy")

ALL_HOURS = HOURS_OF_THE_24_HOUR_CLOCK + HOURS_OF_THE_12_HOUR_CLOCK

DURATION_NUMBER_UNITS = ("seconds", "minutes", "hours")
DURATION_TEXT = "iso"
WEEKDAY_TEXT = "name"
WEEKDAY_NUMBER = "iso"
DAYS_PLACEHOLDER = "{n}"

_LITERALS = frozenset(" :/.,-_+")
"""What may stand between the tokens of a pattern: the separators of a time and of a date."""


class ValueFormat(Contract):
    """One way a skill takes a value that is said in words."""

    kind: FormatKind
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN_CHARS)

    @property
    def group(self) -> FormatGroup:
        return GROUP_OF[self.kind]


def pattern_parts(pattern: str, tokens: Sequence[str]) -> tuple[tuple[str, bool], ...]:
    """The pattern as parts, each ``(text, is_a_token)``, the longest token first (``HH`` before
    ``H``); the separators between tokens are one part each. Raises ``ValueError`` for a character
    that is neither."""
    ordered = sorted(tokens, key=len, reverse=True)
    parts: list[tuple[str, bool]] = []
    index = 0
    while index < len(pattern):
        token = next((t for t in ordered if pattern.startswith(t, index)), None)
        if token is not None:
            parts.append((token, True))
            index += len(token)
            continue
        char = pattern[index]
        if char not in _LITERALS:
            raise ValueError(
                f"{char!r} is not part of the pattern: write it with {', '.join(tokens)} and, "
                f"between them, any of {''.join(sorted(_LITERALS))!r}"
            )
        if parts and not parts[-1][1]:
            parts[-1] = (parts[-1][0] + char, False)
        else:
            parts.append((char, False))
        index += 1
    return tuple(parts)


def _tokens_of(pattern: str, tokens: Sequence[str]) -> list[str]:
    return [text for text, is_token in pattern_parts(pattern, tokens) if is_token]


def _once(found: list[str], group: Sequence[str], what: str) -> str | None:
    """The problem with how many tokens of ``group`` there are among ``found``, if there is one."""
    count = sum(1 for token in found if token in group)
    return None if count == 1 else f"{what} is written once, with one of {', '.join(group)}"


def problem_with_format(fmt: ValueFormat, param_type: str) -> str | None:
    """What is wrong with ``fmt`` for a parameter of ``param_type`` (``string``, ``integer`` or
    ``number``), or nothing when it is right."""
    try:
        return _problem(fmt, param_type)
    except ValueError as error:
        return str(error)


def _problem(fmt: ValueFormat, param_type: str) -> str | None:
    match fmt.kind:
        case FormatKind.TIME:
            if param_type != "string":
                return "a time of day is written as text"
            found = _tokens_of(fmt.pattern, TIME_TOKENS)
            problems = (_once(found, ALL_HOURS, "the hour"), _once(found, ("mm",), "the minutes"))
            if any(problems):
                return next(p for p in problems if p)
            hour = next(t for t in found if t in ALL_HOURS)
            if (hour in HOURS_OF_THE_12_HOUR_CLOCK) != ("a" in found):
                return "a 12-hour clock (hh, h) is written with a (am or pm), a 24-hour one without"
            if found.count("a") > 1:
                return "a (am or pm) is written once"
            if found.index("mm") < found.index(hour):
                return "the hour comes before the minutes"
            return None
        case FormatKind.DATE:
            if param_type != "string":
                return "a date is written as text"
            found = _tokens_of(fmt.pattern, DATE_TOKENS)
            problems = (
                _once(found, DAYS_OF_THE_MONTH, "the day of the month"),
                _once(found, MONTHS, "the month"),
            )
            if any(problems):
                return next(p for p in problems if p)
            if sum(1 for t in found if t in YEARS) > 1:
                return "the year is written at most once, with yyyy or yy"
            return None
        case FormatKind.DURATION:
            if fmt.pattern in DURATION_NUMBER_UNITS:
                if param_type not in {"integer", "number"}:
                    return f"a length in {fmt.pattern} is a number"
                return None
            if fmt.pattern == DURATION_TEXT:
                return None if param_type == "string" else "an iso length (PT1H30M) is text"
            return f"a length is in {', '.join(DURATION_NUMBER_UNITS)} or {DURATION_TEXT}"
        case FormatKind.IN_DAYS:
            if fmt.pattern.count(DAYS_PLACEHOLDER) != 1:
                return f"the number of days is written once, as {DAYS_PLACEHOLDER}"
            rest = fmt.pattern.replace(DAYS_PLACEHOLDER, "")
            if "{" in rest or "}" in rest or not rest.isprintable():
                return f"the only thing in braces is {DAYS_PLACEHOLDER}"
            if param_type == "integer":
                return None if fmt.pattern == DAYS_PLACEHOLDER else "a whole number is {n} alone"
            return None if param_type == "string" else "a number of days is text or a whole number"
        case FormatKind.WEEKDAY:
            if fmt.pattern == WEEKDAY_TEXT:
                return None if param_type == "string" else "the name of a day is text"
            if fmt.pattern == WEEKDAY_NUMBER:
                return None if param_type == "integer" else "the number of a day is a whole number"
            return f"a day of the week is written as {WEEKDAY_TEXT} or {WEEKDAY_NUMBER}"
    return None
