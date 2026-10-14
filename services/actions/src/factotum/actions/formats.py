"""A value that is said in words, written in the form a skill declares (ADR 0030).

A person says *sette e mezza*, *tra tre giorni*, *il 16 ottobre 2026*, and a skill whose manifest
declares ``formats`` wants ``07:30``, ``+3d`` or ``16/10/26``. The model is asked for the words that
were said, and this does the rest, in code, so that nothing is a sum of a model:

1. what the model wrote is **read** (``core.when`` knows how a time, a length and a day are said, in
   Italian and in English, and what the model wrote may be said in either, or be ``7:30``);
2. the message is **searched** for the same thing: the windows of its words that read as a time, a
   length or a day, and of those the ones that are not part of a longer one (*sette* is not said in
   *sette e mezza*, and 7:00 is not what that says);
3. if the model's reading is one of the message's, it is a value that the person said (ADR 0028:
   a made-up *600* for *cinque minuti* is a reading that no window of the message has), and it is
   **written** in the first form the skill declared that can say it.

A form that cannot say it is not used: the platform has no clock, so *domani* is ``+1d`` for a skill
that counts days and nothing at all for one that wants a calendar date, and a date with no year
cannot be written as ``dd/MM/yy``. What cannot be written is not a value (it is *unsaid*: asked
for if the skill requires it), never a guess. Fitting a skill to the calendar is for the day when a
skill can use the clock first (several skills in one request, not built).
"""

import re
from calendar import monthrange
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from factotum.actions.core.when import SaidDay, read_day, read_duration, read_time, tokenize
from factotum.contracts.commands import Weekday
from factotum.contracts.formats import (
    DATE_TOKENS,
    DAYS_PLACEHOLDER,
    DURATION_TEXT,
    TIME_TOKENS,
    WEEKDAY_NUMBER,
    YEARS,
    FormatGroup,
    FormatKind,
    ValueFormat,
    pattern_parts,
)
from factotum.contracts.skills import ParameterSpec, ParamType, ParamValue

WINDOW = 12
"""The most words in which a time, a length or a day is looked for: *alle sette e mezza di sera di
domani* is eight."""

_MASK = "xdatex"
"""What stands in a message where a numeric date was, so that its digits are not read as an hour."""


@dataclass(frozen=True, slots=True)
class TimeOfDay:
    hour: int
    minute: int


@dataclass(frozen=True, slots=True)
class Length:
    seconds: int


Reading = TimeOfDay | Length | SaidDay
"""What words say, by group: an hour, a length of time or a day."""


# ---- numeric dates (16/10/26) --------------------------------------------------------------------

_NOT_PART_OF_MORE = r"(?<![\w/.\-])"
_NUMERIC_DATE = re.compile(
    _NOT_PART_OF_MORE
    + r"(?:(?P<iso_year>\d{4})-(?P<iso_month>\d{1,2})-(?P<iso_day>\d{1,2})"
    + r"|(?P<day>\d{1,2})(?P<sep>[/.\-])(?P<month>\d{1,2})(?:(?P=sep)(?P<year>\d{4}|\d{2}))?)"
    + r"(?!\d|[/.\-]\d)"
)


def _calendar_day(day: int, month: int, year: int | None) -> SaidDay | None:
    """A date, if there is one (31 February is not)."""
    if not 1 <= month <= 12 or (year is not None and not 1900 <= year <= 2200):
        return None
    if not 1 <= day <= monthrange(year if year is not None else 2000, month)[1]:
        return None
    return SaidDay(day=day, month=month, year=year)


def numeric_dates(text: str) -> list[tuple[int, int, SaidDay]]:
    """The dates that ``text`` writes in digits (``16/10/26``, ``16.10.2026``, ``2026-10-16``,
    ``16/10``), each as where it begins, where it ends and what it says: the day comes first, as
    it is written in Italy and most of Europe. Two numbers are a date only with a slash."""
    found: list[tuple[int, int, SaidDay]] = []
    for match in _NUMERIC_DATE.finditer(text):
        year: int | None
        if match["iso_year"] is not None:
            day, month, year = (
                int(match["iso_day"]),
                int(match["iso_month"]),
                int(match["iso_year"]),
            )
        else:
            written_year = match["year"]
            if written_year is None and match["sep"] != "/":
                continue
            day, month = int(match["day"]), int(match["month"])
            year = None
            if written_year is not None:
                year = int(written_year) + (2000 if len(written_year) == 2 else 0)
        date = _calendar_day(day, month, year)
        if date is not None:
            found.append((match.start(), match.end(), date))
    return found


def _masked(text: str) -> tuple[str, list[SaidDay]]:
    """``text`` with its numeric dates taken out (and said apart), so that ``16/10/26`` is not an
    hour and a year."""
    dates = numeric_dates(text)
    for start, end, _ in reversed(dates):
        text = f"{text[:start]} {_MASK} {text[end:]}"
    return text, [date for _, _, date in dates]


# ---- reading words -------------------------------------------------------------------------------


def _time(words: list[str]) -> Reading | None:
    found = read_time(words)
    return None if found is None else TimeOfDay(*found)


def _length(words: list[str]) -> Reading | None:
    seconds = read_duration(words)
    return None if seconds is None else Length(seconds)


_READ: dict[FormatGroup, Callable[[list[str]], Reading | None]] = {
    FormatGroup.TIME: _time,
    FormatGroup.DURATION: _length,
    FormatGroup.DAY: read_day,
}


def _words(text: str, group: FormatGroup) -> list[str]:
    return tokenize(text, decimals=group is FormatGroup.DURATION)


Span = tuple[int, int, Reading]
"""Where in the words something is said (where it begins, where it ends) and what it says."""


def _contained(span: Span, other: Span) -> bool:
    return other[0] <= span[0] and span[1] <= other[1]


def _inside(span: Span, other: Span) -> bool:
    return _contained(span, other) and span[:2] != other[:2]


def _spans(words: list[str], read: Callable[[list[str]], Reading | None]) -> list[Span]:
    """What the words say, wherever they say it: the longest window that reads from each word, less
    those that are inside another. The inside of a longer one is not said (*sette* in *sette e
    mezza*)."""
    found: list[Span] = []
    for start in range(len(words)):
        for end in range(min(len(words), start + WINDOW), start, -1):
            reading = read(words[start:end])
            if reading is not None:
                found.append((start, end, reading))
                break
    return [span for span in found if not any(_inside(span, other) for other in found)]


@lru_cache(maxsize=256)
def said_in(message: str, group: FormatGroup) -> frozenset[Reading]:
    """Every time, length or day (by ``group``) that ``message`` says."""
    masked, dates = _masked(message)
    words = _words(masked, group)
    spans = _spans(words, _READ[group])
    if group is FormatGroup.TIME:
        # the *dieci* of *tra dieci minuti* and the *due* of *per due ore* are numbers of a length
        lengths = _spans(words, _length)
        spans = [span for span in spans if not any(_contained(span, length) for length in lengths)]
    found = {reading for _, _, reading in spans}
    if group is FormatGroup.DAY:
        found.update(dates)
    return frozenset(found)


_AROUND_A_DATE = frozenset({"il", "l", "the", "on", "of", "di", "del", "dal", "al", "per", "for"})
"""What a model may write in front of a date in digits (*il 16/10/26*) and mean nothing by it."""


def read_value(text: str, group: FormatGroup) -> Reading | None:
    """What a value that a model wrote says, whole: nothing when it says more than one thing, or
    what is not understood. A day may be written in digits (``16/10/26``), and then it is that
    date and nothing else: digits that are a date are not an hour and a year."""
    masked, dates = _masked(text)
    if dates:
        around = [word for word in _words(masked, group) if word != _MASK]
        alone = len(dates) == 1 and all(word in _AROUND_A_DATE for word in around)
        return dates[0] if group is FormatGroup.DAY and alone else None
    return _READ[group](_words(text, group))


# ---- writing a form ------------------------------------------------------------------------------


def _write_time(pattern: str, said: TimeOfDay) -> str:
    twelve = said.hour % 12 or 12
    fields = {
        "HH": f"{said.hour:02d}",
        "H": str(said.hour),
        "hh": f"{twelve:02d}",
        "h": str(twelve),
        "mm": f"{said.minute:02d}",
        "a": "am" if said.hour < 12 else "pm",
    }
    return "".join(
        fields[text] if is_token else text for text, is_token in pattern_parts(pattern, TIME_TOKENS)
    )


def _write_date(pattern: str, said: SaidDay) -> str | None:
    if said.day is None or said.month is None:
        return None
    parts = pattern_parts(pattern, DATE_TOKENS)
    if any(text in YEARS for text, is_token in parts if is_token) and said.year is None:
        return None
    year = said.year or 0
    fields = {
        "dd": f"{said.day:02d}",
        "d": str(said.day),
        "MM": f"{said.month:02d}",
        "M": str(said.month),
        "yyyy": str(year),
        "yy": f"{year % 100:02d}",
    }
    return "".join(fields[text] if is_token else text for text, is_token in parts)


def _write_length(unit: str, seconds: int, param_type: ParamType) -> ParamValue | None:
    if unit == DURATION_TEXT:
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        parts = [f"{n}{letter}" for n, letter in ((hours, "H"), (minutes, "M"), (secs, "S")) if n]
        return "PT" + ("".join(parts) or "0S")
    size = {"seconds": 1, "minutes": 60, "hours": 3600}[unit]
    if seconds % size == 0:
        return seconds // size
    if param_type is ParamType.INTEGER:
        return None  # 90 seconds are not a whole number of minutes
    return round(seconds / size, 4)


def write(fmt: ValueFormat, said: Reading, param_type: ParamType) -> ParamValue | None:
    """``said`` in the form ``fmt`` declares, or nothing when that form cannot say it."""
    match fmt.kind, said:
        case FormatKind.TIME, TimeOfDay():
            return _write_time(fmt.pattern, said)
        case FormatKind.DURATION, Length():
            return _write_length(fmt.pattern, said.seconds, param_type)
        case FormatKind.DATE, SaidDay():
            return _write_date(fmt.pattern, said)
        case FormatKind.IN_DAYS, SaidDay(in_days=int() as days):
            return (
                days
                if param_type is ParamType.INTEGER
                else fmt.pattern.replace(DAYS_PLACEHOLDER, str(days))
            )
        case FormatKind.WEEKDAY, SaidDay(weekday=Weekday() as weekday):
            if fmt.pattern == WEEKDAY_NUMBER:
                return list(Weekday).index(weekday) + 1
            return weekday.value
    return None


# ---- what a skill is given -----------------------------------------------------------------------


def _text(raw: object) -> str | None:
    """What a model wrote, as text: a whole number is the text of it (an hour: 7). A yes, a number
    with a decimal point and anything that is not text are not an hour or a length or a day."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw)
    return raw if isinstance(raw, str) else None


def convert(spec: ParameterSpec, raw: object, message: str) -> ParamValue | None:
    """What a skill is given for ``raw``, a value that a model wrote for a parameter that declares
    formats: the value in the form that can say it, if the person said it (a day is a date, a
    number of days or a day of the week, and each is written by one form at most). Nothing when it
    is not understood, when it was not said, or when no form can say it."""
    if not spec.formats:
        return None
    group = spec.formats[0].group
    text = _text(raw)
    if text is None:
        return None
    meant = read_value(text, group)
    if meant is None or meant not in said_in(message, group):
        return None
    for fmt in spec.formats:
        written = write(fmt, meant, spec.type)
        if written is not None:
            return written
    return None
