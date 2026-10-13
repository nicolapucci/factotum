"""What a person says of time, read by code (ADR 0029): how long a timer lasts (*five minutes*, *an
hour and a half*, *mezz'ora*, *2h30*), and the hour (*alle sette e mezza*, *half past seven*,
*7:30 pm*) and the day (*tomorrow*, *lunedì*, *il 15 ottobre*) of an alarm, in English and Italian.

A model copies the words of the person (the check of ADR 0028 finds them in the message) and does no
sums: *300* for *five minutes* is a number that was not said. So the sums are done here, in code,
and the platform needs no clock for them: an hour and a minute are not a moment, and a day is read
as *a way of saying a day* (``AlarmDay``) that the device, which has the calendar, turns into one.

A reader is strict. A word that it does not know is not skipped, unless it is one of those that a
person says around a time without meaning anything by it (*at*, *alle*, *the*, *for*): whatever else
is there might have changed what was meant (*in two weeks*, *every day*), and what is not understood
is asked again, not guessed. Both languages are read whichever the person speaks, since they mix.
"""

import re
import unicodedata
from dataclasses import dataclass

from factotum.actions.grounding import number_at
from factotum.contracts.commands import AlarmDay, Weekday

# ---- words ---------------------------------------------------------------------------------------

_AND = frozenset({"e", "and"})
_ARTICLES = frozenset({"un", "uno", "una", "a", "an"})
_OF = frozenset({"d", "di", "of"})
_HALF = frozenset({"mezz", "mezza", "mezzo", "half"})
_QUARTER = frozenset({"quarto", "quarter"})
_QUARTERS = frozenset({"quarti", "quarters"})

_SECONDS = frozenset({"secondo", "secondi", "sec", "secs", "s", "second", "seconds"})
_MINUTES = frozenset({"minuto", "minuti", "min", "mins", "m", "minute", "minutes"})
_HOURS = frozenset({"ora", "ore", "h", "hr", "hrs", "hour", "hours"})
_DAYS = frozenset({"giorno", "giorni", "day", "days"})
_WEEKS = frozenset({"settimana", "settimane", "week", "weeks"})

_FOR_A_DURATION = frozenset(
    {"tra", "fra", "per", "da", "di", "in", "il", "lo", "la", "un", "una", "uno", "timer"}
    | {"for", "of", "the", "a", "an", "to", "about", "circa"}
)
"""What is said around a length of time without meaning anything by it."""

# ---- the tokens ----------------------------------------------------------------------------------

_WITH_DECIMALS = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]+")
_WHOLE = re.compile(r"\d+|[^\W\d_]+")
_DAY_PERIOD = re.compile(r"\b([ap])\.\s?m\b\.?")


def _plain(text: str) -> str:
    """Lower case, with the accents taken off (a word heard with the wrong accent is still the
    word) and *p.m.* written *pm*."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(letter for letter in decomposed if not unicodedata.combining(letter))
    return _DAY_PERIOD.sub(r"\1m", letters)


def _tokens(text: str, *, decimals: bool) -> list[str]:
    """Numbers and words: ``2h30`` is ``2``, ``h``, ``30``; ``7:30`` is ``7``, ``30``. A number with
    a decimal point is one token when ``decimals`` (a length of time: ``1,5 hours``) and two when it
    is not (an hour: ``7.30``)."""
    return (_WITH_DECIMALS if decimals else _WHOLE).findall(_plain(text))


def _is_decimal(word: str) -> bool:
    return word[:1].isdigit() and not word.isdigit()


# ---- how long ------------------------------------------------------------------------------------


def _unit(word: str) -> int | None:
    """How many seconds a unit of time is, or nothing when the word is not one."""
    if word in _SECONDS:
        return 1
    if word in _MINUTES:
        return 60
    if word in _HOURS:
        return 3600
    return None


def _unit_at(words: list[str], index: int) -> int | None:
    return _unit(words[index]) if index < len(words) else None


def _quantity(words: list[str], index: int) -> tuple[float, int] | None:
    """A number (in digits, with a decimal point, or in words) or an article, as a quantity, and
    where it ends."""
    word = words[index]
    if _is_decimal(word):
        return float(word.replace(",", ".")), index + 1
    found = number_at(words, index)
    if found is not None:
        return float(found[0]), index + found[1]
    if word in _ARTICLES:
        return 1.0, index + 1
    return None


def _fraction(words: list[str], index: int) -> tuple[int, int] | None:
    """A number of quarters (*un quarto*, *tre quarti*, *a quarter*), and where they end."""
    word = words[index]
    if word in _QUARTER:
        return 1, index + 1
    if index + 1 < len(words) and words[index + 1] in _QUARTER | _QUARTERS:
        if word in _ARTICLES:
            return 1, index + 2
        found = number_at(words, index)
        if found is not None and found[1] == 1:
            return found[0], index + 2
    return None


def _half_or_quarter_term(
    words: list[str], index: int, last: int | None
) -> tuple[float, int, int] | None:
    """*Half an hour*, *mezz'ora*, *un quarto d'ora*, *tre quarti d'ora*; and, after a length,
    *and a half* or *e un quarto* of the unit that it was in."""
    size = len(words)
    start = index
    if words[index] in _ARTICLES and index + 1 < size and words[index + 1] in _HALF:
        start = index + 1  # a half hour
    if words[start] in _HALF:
        amount, end = 0.5, start + 1
    else:
        quarters = _fraction(words, index)
        if quarters is None:
            return None
        amount, end = quarters[0] * 0.25, quarters[1]
    after = end
    if after < size and words[after] in _OF:
        after += 1
    if after < size and words[after] in _ARTICLES:
        after += 1
    unit = _unit_at(words, after)
    if unit is not None:
        return amount * unit, unit, after + 1
    return None if last is None else (amount * last, last, end)


def _term(words: list[str], index: int, last: int | None) -> tuple[float, int, int] | None:
    """A piece of a length of time that starts at ``index``: the seconds of it, the unit it is in
    (what *and a half* and a number alone that follow are of), and where it ends. ``last`` is the
    unit of the piece before, if there was one."""
    size = len(words)
    fraction = _half_or_quarter_term(words, index, last)
    if fraction is not None:
        return fraction
    quantity = _quantity(words, index)
    if quantity is None:
        return None
    value, end = quantity
    # two and a half minutes, due ore e un quarto
    joined = end + 1 if end < size and words[end] in _AND else end
    if joined + 1 < size and words[joined] in _ARTICLES and words[joined + 1] in _HALF | _QUARTER:
        joined += 1
    if joined + 1 < size and words[joined] in _HALF | _QUARTER:
        unit = _unit(words[joined + 1])
        if unit is not None:
            return (value + (0.5 if words[joined] in _HALF else 0.25)) * unit, unit, joined + 2
    unit = _unit_at(words, end)
    if unit is not None:  # five minutes, un'ora
        return value * unit, unit, end + 1
    if words[index] in _ARTICLES or last is None or last == 1:
        return None
    # a number alone after a length is the next unit down: 2h30 is two hours and thirty minutes
    smaller = 60 if last == 3600 else 1
    return value * smaller, smaller, end


def parse_duration(text: str) -> int | None:
    """How many seconds the words say, or nothing when they are not a length of time that is
    understood: *five minutes*, *un'ora e mezza*, *half an hour*, *2h30*, *a quarter of an hour*."""
    words = _tokens(text, decimals=True)
    total, last, index, found = 0.0, None, 0, False
    while index < len(words):
        if words[index] in _AND:
            index += 1
            continue
        step = _term(words, index, last)
        if step is None:
            if words[index] not in _FOR_A_DURATION:
                return None
            index += 1
            continue
        seconds, last, index = step
        total += seconds
        found = True
    return round(total) if found else None


# ---- the hour and the day of an alarm -----------------------------------------------------------

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("gennaio", "january"),
            ("febbraio", "february"),
            ("marzo", "march"),
            ("aprile", "april"),
            ("maggio", "may"),
            ("giugno", "june"),
            ("luglio", "july"),
            ("agosto", "august"),
            ("settembre", "september", "sept"),
            ("ottobre", "october"),
            ("novembre", "november"),
            ("dicembre", "december"),
        ),
        1,
    )
    for name in names
}

_WEEKDAYS = {
    **dict.fromkeys(("lunedi", "monday"), Weekday.MONDAY),
    **dict.fromkeys(("martedi", "tuesday"), Weekday.TUESDAY),
    **dict.fromkeys(("mercoledi", "wednesday"), Weekday.WEDNESDAY),
    **dict.fromkeys(("giovedi", "thursday"), Weekday.THURSDAY),
    **dict.fromkeys(("venerdi", "friday"), Weekday.FRIDAY),
    **dict.fromkeys(("sabato", "saturday"), Weekday.SATURDAY),
    **dict.fromkeys(("domenica", "sunday"), Weekday.SUNDAY),
}

_ORDINAL_WORDS = (
    *("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"),
    *("tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth"),
    *("seventeenth", "eighteenth", "nineteenth", "twentieth"),
)
_ORDINALS = {
    **{word: number for number, word in enumerate(_ORDINAL_WORDS, 1)},
    "thirtieth": 30,
    "primo": 1,
}
_ORDINAL_SUFFIXES = frozenset({"st", "nd", "rd", "th"})
_DATE_MARKERS = frozenset({"il", "the", "on", "l"})

# The period of the day: what makes an hour that is said as 7 the one of the morning or the evening.
_MORNING = frozenset({"am", "mattina", "mattino", "morning"})
_AFTERNOON = frozenset({"pm", "pomeriggio", "sera", "afternoon", "evening"})
_NIGHT = frozenset({"notte", "night"})

_TODAY = frozenset({"oggi", "today"})
_TOMORROW = frozenset({"domani", "tomorrow"})
_WITH_A_PERIOD = {
    "stasera": ("pm", 0),
    "stamattina": ("am", 0),
    "stanotte": ("night", 0),
    "tonight": ("night", 0),
    "domattina": ("am", 1),
}

_MIDNIGHT = frozenset({"mezzanotte", "midnight"})
_NOON = frozenset({"mezzogiorno", "noon", "midday"})
_PAST = frozenset({"past", "after"})
_TO = frozenset({"to", "before"})
_IN = frozenset({"tra", "fra", "in"})

_FOR_AN_ALARM = frozenset(
    {"alle", "all", "le", "l", "ore", "ora", "il", "lo", "la", "del", "della", "di", "a", "al"}
    | {"per", "nel", "nella", "sveglia", "sul", "sulle", "tra", "fra", "e", "and"}
    | {"at", "on", "the", "of", "for", "o", "clock", "oclock", "in"}
    | {"next", "prossimo", "prossima", "this", "questo", "questa", "alarm"}
)
"""What is said around the hour and the day of an alarm without meaning anything by it."""


@dataclass(frozen=True, slots=True)
class AlarmWords:
    """What the words of a person say of an alarm: the hour and the minute (on the 24-hour clock),
    and the way they said the day, which is none when they did not."""

    hour: int
    minute: int
    day: AlarmDay


@dataclass(slots=True)
class _Reading:
    clock: tuple[int, int] | None = None
    day: AlarmDay | None = None
    period: str | None = None


def _hour_number(words: list[str], index: int) -> tuple[int, int] | None:
    """An hour said as a number: *7*, *sette*, *diciannove*, and *all'una*. Whether it is an hour
    of the day is for ``_on_the_24_hour_clock`` to say, once, for every way of saying one."""
    if index >= len(words):
        return None
    if words[index] == "una" and words[index - 1 : index] in (["all"], ["l"]):
        return 1, 1
    return number_at(words, index)


def _before_a_date_marker(words: list[str], index: int) -> bool:
    """Whether the number at ``index`` is a day of the month: a month or an ordinal follows it."""
    following = words[index + 1 : index + 2]
    after_of = words[index + 2 : index + 3] if following and following[0] in _OF else []
    return bool(
        (following and (following[0] in _MONTHS or following[0] in _ORDINAL_SUFFIXES))
        or (after_of and after_of[0] in _MONTHS)
    )


def _day_number(words: list[str], index: int) -> tuple[int, int] | None:
    """A day of the month: a number from 1 to 31, with an ordinal suffix (*15th*), or an ordinal
    word (*fifteenth*, *twenty first*, *il primo*)."""
    if index >= len(words):
        return None
    word = words[index]
    if word in _ORDINALS:
        return _ORDINALS[word], index + 1
    found = number_at(words, index)
    if found is None:
        return None
    number, end = found[0], index + found[1]
    if number in (20, 30) and end < len(words) and 1 <= _ORDINALS.get(words[end], 0) <= 9:
        number, end = number + _ORDINALS[words[end]], end + 1
    if not 1 <= number <= 31:
        return None
    if end < len(words) and words[end] in _ORDINAL_SUFFIXES:
        end += 1
    return number, end


def _date_at(words: list[str], index: int) -> tuple[AlarmDay, int] | None:
    """A date: *il 15 ottobre*, *15 ottobre*, *october 15th*, *the 15th*, *il primo maggio*, *il
    15*, and where it ends. A number alone is an hour, not a date."""
    size = len(words)
    if words[index] in _MONTHS:  # october 15
        found = _day_number(words, index + 1)
        if found is not None:
            return AlarmDay(day=found[0], month=_MONTHS[words[index]]), found[1]
        return None
    marker = index
    while marker < size and words[marker] in _DATE_MARKERS:
        marker += 1
    found = _day_number(words, marker)
    if found is None:
        return None
    number, end = found
    after = end + 1 if end < size and words[end] in _OF else end
    if after < size and words[after] in _MONTHS:
        return AlarmDay(day=number, month=_MONTHS[words[after]]), after + 1
    if marker > index or words[end - 1] in _ORDINAL_SUFFIXES or words[marker] in _ORDINALS:
        return AlarmDay(day=number), end  # il 15, the 15th, il primo
    return None


def _relative_day(words: list[str], index: int) -> tuple[AlarmDay, int] | None:
    """*In three days*, *tra due settimane*."""
    size = len(words)
    if words[index] not in _IN or index + 2 >= size:
        return None
    found = number_at(words, index + 1)
    if found is None and words[index + 1] in _ARTICLES:
        found = (1, 1)
    if found is None:
        return None
    end = index + 1 + found[1]
    unit = words[end] if end < size else ""
    if unit in _DAYS and found[0] <= 366:
        return AlarmDay(in_days=found[0]), end + 1
    if unit in _WEEKS and found[0] * 7 <= 366:
        return AlarmDay(in_days=found[0] * 7), end + 1
    return None


def _day_at(words: list[str], index: int) -> tuple[AlarmDay, str | None, int] | None:
    """A day, in the way a person says one: the period of the day that goes with it (*stasera*),
    and where it ends."""
    word = words[index]
    if word in _TODAY:
        return AlarmDay(in_days=0), None, index + 1
    if word in _TOMORROW:
        return AlarmDay(in_days=1), None, index + 1
    if word in _WITH_A_PERIOD:
        period, days = _WITH_A_PERIOD[word]
        return AlarmDay(in_days=days), period, index + 1
    if word == "dopodomani":
        return AlarmDay(in_days=2), None, index + 1
    if word == "dopo" and words[index + 1 : index + 2] == ["domani"]:
        return AlarmDay(in_days=2), None, index + 2
    if words[index : index + 3] == ["day", "after", "tomorrow"]:
        return AlarmDay(in_days=2), None, index + 3
    if word in _WEEKDAYS:
        return AlarmDay(weekday=_WEEKDAYS[word]), None, index + 1
    relative = _relative_day(words, index)
    if relative is not None:
        return relative[0], None, relative[1]
    date = _date_at(words, index)
    if date is not None:
        return date[0], None, date[1]
    return None


def _offset(words: list[str], index: int) -> tuple[int, int] | None:
    """The minutes that an hour is said to be *past* or *to* (*half past*, *a quarter to*, *ten
    past*), and where the word that says which is."""
    size = len(words)
    word = words[index]
    if word in _HALF:
        minutes, relation = 30, index + 1
    elif word in _QUARTER:
        minutes, relation = 15, index + 1
    elif word in _ARTICLES and index + 1 < size and words[index + 1] in _QUARTER:
        minutes, relation = 15, index + 2
    else:
        found = number_at(words, index)
        if found is None:
            return None
        minutes, relation = found[0], index + found[1]
        if relation < size and words[relation] in {"minutes", "minuti"}:
            relation += 1
    if relation < size and words[relation] in _PAST | _TO and 0 < minutes < 60:
        return minutes, relation
    return None


def _clock_at(words: list[str], index: int) -> tuple[tuple[int, int], int] | None:
    """An hour and a minute, as the words say them, and where they end: *alle sette e mezza*,
    *7:30*, *half past seven*, *quarter to eight*, *sette meno un quarto*, *mezzogiorno*."""
    size = len(words)
    word = words[index]
    if word in _MIDNIGHT:
        return (0, 0), index + 1
    if word in _NOON:
        return (12, 0), index + 1

    offset = _offset(words, index)  # half past seven, a quarter to eight, ten past seven
    if offset is not None:
        minutes, relation = offset
        found = _hour_number(words, relation + 1)
        if found is not None:
            end = relation + 1 + found[1]
            if words[relation] in _PAST:
                return (found[0], minutes), end
            return ((found[0] - 1) % 24, 60 - minutes), end
        return None

    found = _hour_number(words, index)
    if found is None or _before_a_date_marker(words, index):
        return None
    hour, end = found[0], index + found[1]
    cursor = end + 1 if end < size and words[end] in _AND else end
    if cursor >= size:
        # an hour and then a word that joins it to something, and nothing: cut short
        return None if cursor > end else ((hour, 0), end)
    if words[cursor] in _HALF:  # sette e mezza
        return (hour, 30), cursor + 1
    quarters = _fraction(words, cursor)
    if quarters is not None:  # sette e un quarto, sette e tre quarti
        return (hour, quarters[0] * 15), quarters[1]
    if words[cursor] == "meno":  # sette meno un quarto, sette meno dieci
        return _before_the_hour(words, hour, cursor + 1)
    minute = None if _before_a_date_marker(words, cursor) else number_at(words, cursor)
    if minute is not None:  # 7:30, 7 e 30, seven thirty
        after = cursor + minute[1]
        if after < size and words[after] in {"minuti", "minutes"}:
            after += 1
        return (hour, minute[0]), after
    return (hour, 0), end


def _before_the_hour(words: list[str], hour: int, index: int) -> tuple[tuple[int, int], int] | None:
    """*Meno un quarto* or *meno dieci*, said after an hour, and where it ends."""
    if index >= len(words):
        return None
    quarters = _fraction(words, index)
    if quarters is not None:
        return ((hour - 1) % 24, 60 - quarters[0] * 15), quarters[1]
    minutes = number_at(words, index)
    if minutes is not None and 0 < minutes[0] < 60:
        return ((hour - 1) % 24, 60 - minutes[0]), index + minutes[1]
    return None


def _on_the_24_hour_clock(hour: int, minute: int, period: str | None) -> tuple[int, int] | None:
    """The hour that the period makes of the one that was said, or nothing when they do not fit
    (*15 am*)."""
    if not 0 <= hour <= 24 or not 0 <= minute < 60 or (hour == 24 and minute):
        return None
    hour %= 24
    if period == "pm" and hour < 12:
        hour += 12
    elif period == "am":
        if hour > 12:
            return None
        hour = 0 if hour == 12 else hour
    elif period == "night":
        if hour == 12:
            hour = 0
        elif 6 <= hour < 12:
            hour += 12
    return hour, minute


def _is_a_length_from_now(words: list[str], index: int) -> bool:
    """*In ten minutes*, *tra due ore*: a time that is counted from now, which is a timer and not
    an alarm (the platform has no clock to count it from)."""
    if words[index] not in _IN or index + 2 >= len(words):
        return False
    found = number_at(words, index + 1) or ((1, 1) if words[index + 1] in _ARTICLES else None)
    return found is not None and _unit_at(words, index + 1 + found[1]) is not None


def parse_alarm(text: str) -> AlarmWords | None:
    """The hour and the day that the words say, or nothing when they say no hour, say two of
    something, or say what is not understood. *Domani alle sette e mezza*, *lunedì 6:45*, *half past
    seven pm on the 15th of October*."""
    words = _tokens(text, decimals=False)
    reading = _Reading()
    index = 0
    while index < len(words):
        if _is_a_length_from_now(words, index):
            return None
        step = _read(words, index, reading)
        if step is None:
            if words[index] not in _FOR_AN_ALARM:
                return None
            index += 1
            continue
        index = step
    if reading.clock is None:
        return None
    hour = _on_the_24_hour_clock(*reading.clock, reading.period)
    if hour is None:
        return None
    return AlarmWords(hour[0], hour[1], reading.day or AlarmDay())


def _read(words: list[str], index: int, reading: _Reading) -> int | None:
    """Take what is at ``index`` into the reading, and say where it ends; nothing when it is none
    of what is looked for, or when it is a second of something that is said once."""
    word = words[index]
    if word in _MORNING | _AFTERNOON | _NIGHT:
        said_period = "am" if word in _MORNING else "pm" if word in _AFTERNOON else "night"
        if reading.period not in (None, said_period):
            return None
        reading.period = said_period
        return index + 1
    day = _day_at(words, index)
    if day is not None:
        said, with_period, end = day
        if reading.day is not None or (with_period and reading.period not in (None, with_period)):
            return None
        reading.day = said
        reading.period = with_period or reading.period
        return end
    clock = _clock_at(words, index)
    if clock is not None and reading.clock is None:
        reading.clock = clock[0]
        return clock[1]
    return None
