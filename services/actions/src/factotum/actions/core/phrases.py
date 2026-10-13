"""What the skills of the platform that act on a device say of what they did, in the language of the
person (ADR 0029).

A sentence is chosen by the code and not written by a model, as every sentence about an action is
(``factotum.actions.phrases``): the same each time, and checked in each language by a test. English
and Italian are here; another language is answered in English until somebody adds it.

So are the words for a length of time, an hour and a day (*5 minutes*, *alle 7 e 30*, *domani*): a
device answers with numbers and the skill that asked says them.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from factotum.actions.phrases import language_of


class CoreSay(StrEnum):
    VOLUME_AT = "volume_at"
    """The volume was set, or turned, and this is where it is."""

    MUTED = "muted"
    UNMUTED = "unmuted"

    SOUND_AT = "sound_at"
    """How it is, when the sound is on."""

    SOUND_MUTED = "sound_muted"
    """How it is, when the sound is muted."""

    # ---- timers
    DURATION_UNCLEAR = "duration_unclear"
    DURATION_RANGE = "duration_range"
    TIMER_SET = "timer_set"
    TIMER_RING = "timer_ring"
    """What a timer says when it is up: it is made a voice when the timer is set."""

    TIMERS_FULL = "timers_full"
    TIMER_CANCELLED = "timer_cancelled"
    TIMERS_CANCELLED = "timers_cancelled"
    TIMER_NONE = "timer_none"
    TIMER_NONE_NAMED = "timer_none_named"
    TIMERS_NONE = "timers_none"
    TIMER_LEFT = "timer_left"
    TIMER_RINGING = "timer_ringing"
    TIMERS_MANY = "timers_many"
    TIMER_ITEM_LEFT = "timer_item_left"
    TIMER_ITEM_RINGING = "timer_item_ringing"

    # ---- alarms
    TIME_UNCLEAR = "time_unclear"
    ALARM_SET = "alarm_set"
    ALARM_RING = "alarm_ring"
    """What an alarm says when it rings: it is made a voice when the alarm is set."""

    ALARMS_FULL = "alarms_full"
    ALARM_PAST = "alarm_past"
    ALARM_NO_SUCH_DAY = "alarm_no_such_day"
    ALARM_CANCELLED = "alarm_cancelled"
    ALARMS_CANCELLED = "alarms_cancelled"
    ALARM_NONE = "alarm_none"
    ALARM_NONE_NAMED = "alarm_none_named"
    ALARMS_NONE = "alarms_none"
    ALARM_NEXT = "alarm_next"
    ALARM_RINGING = "alarm_ringing"
    ALARMS_MANY = "alarms_many"
    ALARM_ITEM_RINGING = "alarm_item_ringing"


_CATALOG: dict[str, dict[CoreSay, str]] = {
    "en": {
        CoreSay.VOLUME_AT: "Volume at {level} percent.",
        CoreSay.MUTED: "Muted.",
        CoreSay.UNMUTED: "The sound is back.",
        CoreSay.SOUND_AT: "The volume is at {level} percent.",
        CoreSay.SOUND_MUTED: "The sound is muted. The volume is at {level} percent.",
        CoreSay.DURATION_UNCLEAR: "I did not understand how long that is.",
        CoreSay.DURATION_RANGE: "A timer can last from one second to a day.",
        CoreSay.TIMER_SET: "Timer set for {duration}.",
        CoreSay.TIMER_RING: "Your timer for {duration} is done.",
        CoreSay.TIMERS_FULL: "There are too many timers already.",
        CoreSay.TIMER_CANCELLED: "The timer for {duration} is cancelled.",
        CoreSay.TIMERS_CANCELLED: "{count} timers are cancelled.",
        CoreSay.TIMER_NONE: "There is no timer to cancel.",
        CoreSay.TIMER_NONE_NAMED: "There is no timer for {duration}.",
        CoreSay.TIMERS_NONE: "There are no timers.",
        CoreSay.TIMER_LEFT: "The timer has {left} left.",
        CoreSay.TIMER_RINGING: "The timer is ringing.",
        CoreSay.TIMERS_MANY: "You have {count} timers: {list}.",
        CoreSay.TIMER_ITEM_LEFT: "{left} left",
        CoreSay.TIMER_ITEM_RINGING: "one is ringing",
        CoreSay.TIME_UNCLEAR: "I did not understand what time that is.",
        CoreSay.ALARM_SET: "Alarm set for {when}.",
        CoreSay.ALARM_RING: "Wake up! It is time.",
        CoreSay.ALARMS_FULL: "There are too many alarms already.",
        CoreSay.ALARM_PAST: "That time has already gone by.",
        CoreSay.ALARM_NO_SUCH_DAY: "There is no such day.",
        CoreSay.ALARM_CANCELLED: "The alarm for {when} is cancelled.",
        CoreSay.ALARMS_CANCELLED: "{count} alarms are cancelled.",
        CoreSay.ALARM_NONE: "There is no alarm to cancel.",
        CoreSay.ALARM_NONE_NAMED: "There is no alarm {at}.",
        CoreSay.ALARMS_NONE: "There are no alarms.",
        CoreSay.ALARM_NEXT: "Your alarm is set for {when}.",
        CoreSay.ALARM_RINGING: "The alarm is ringing.",
        CoreSay.ALARMS_MANY: "You have {count} alarms: {list}.",
        CoreSay.ALARM_ITEM_RINGING: "one is ringing",
    },
    "it": {
        CoreSay.VOLUME_AT: "Volume al {level} per cento.",
        CoreSay.MUTED: "Audio in muto.",
        CoreSay.UNMUTED: "Audio riattivato.",
        CoreSay.SOUND_AT: "Il volume è al {level} per cento.",
        CoreSay.SOUND_MUTED: "L'audio è in muto. Il volume è al {level} per cento.",
        CoreSay.DURATION_UNCLEAR: "Non ho capito di quanto tempo parli.",
        CoreSay.DURATION_RANGE: "Un timer può durare da un secondo a un giorno.",
        CoreSay.TIMER_SET: "Timer impostato per {duration}.",
        CoreSay.TIMER_RING: "Il timer da {duration} è scaduto.",
        CoreSay.TIMERS_FULL: "Ci sono già troppi timer.",
        CoreSay.TIMER_CANCELLED: "Timer da {duration} annullato.",
        CoreSay.TIMERS_CANCELLED: "Ho annullato {count} timer.",
        CoreSay.TIMER_NONE: "Non c'è nessun timer da annullare.",
        CoreSay.TIMER_NONE_NAMED: "Non c'è nessun timer da {duration}.",
        CoreSay.TIMERS_NONE: "Non ci sono timer.",
        CoreSay.TIMER_LEFT: "Al timer mancano {left}.",
        CoreSay.TIMER_RINGING: "Il timer sta suonando.",
        CoreSay.TIMERS_MANY: "Hai {count} timer: {list}.",
        CoreSay.TIMER_ITEM_LEFT: "tra {left}",
        CoreSay.TIMER_ITEM_RINGING: "uno sta suonando",
        CoreSay.TIME_UNCLEAR: "Non ho capito che ora intendi.",
        CoreSay.ALARM_SET: "Sveglia impostata per {when}.",
        CoreSay.ALARM_RING: "Sveglia! È ora.",
        CoreSay.ALARMS_FULL: "Ci sono già troppe sveglie.",
        CoreSay.ALARM_PAST: "Quell'ora è già passata.",
        CoreSay.ALARM_NO_SUCH_DAY: "Quel giorno non esiste.",
        CoreSay.ALARM_CANCELLED: "Sveglia per {when} annullata.",
        CoreSay.ALARMS_CANCELLED: "Ho annullato {count} sveglie.",
        CoreSay.ALARM_NONE: "Non c'è nessuna sveglia da annullare.",
        CoreSay.ALARM_NONE_NAMED: "Non c'è nessuna sveglia {at}.",
        CoreSay.ALARMS_NONE: "Non ci sono sveglie.",
        CoreSay.ALARM_NEXT: "La sveglia è per {when}.",
        CoreSay.ALARM_RINGING: "La sveglia sta suonando.",
        CoreSay.ALARMS_MANY: "Hai {count} sveglie: {list}.",
        CoreSay.ALARM_ITEM_RINGING: "una sta suonando",
    },
}

LANGUAGES = frozenset(_CATALOG)


def core_say(language: str | None, key: CoreSay, /, **fields: str | int) -> str:
    """A sentence, in the language if it is known and in English if it is not."""
    return _CATALOG[language_of(language)][key].format_map(fields)


# ---- a length of time, an hour, a day ------------------------------------------------------------

_UNITS = {
    "en": {
        "hour": ("hour", "hours"),
        "minute": ("minute", "minutes"),
        "second": ("second", "seconds"),
    },
    "it": {
        "hour": ("ora", "ore"),
        "minute": ("minuto", "minuti"),
        "second": ("secondo", "secondi"),
    },
}
_AND = {"en": "and", "it": "e"}

_WEEKDAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
}
_MONTHS = {
    "en": (
        *("January", "February", "March", "April", "May", "June", "July"),
        *("August", "September", "October", "November", "December"),
    ),
    "it": (
        *("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio"),
        *("agosto", "settembre", "ottobre", "novembre", "dicembre"),
    ),
}


def say_duration(language: str | None, seconds: int) -> str:
    """A length of time in words, with digits for the numbers: *5 minutes*, *1 ora e 30 minuti*,
    *1 hour, 5 minutes and 10 seconds*."""
    spoken = language_of(language)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{count} {_UNITS[spoken][unit][0 if count == 1 else 1]}"
        for count, unit in ((hours, "hour"), (minutes, "minute"), (secs, "second"))
        if count
    ]
    if not parts:
        return f"0 {_UNITS[spoken]['second'][1]}"
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} {_AND[spoken]} {parts[-1]}"


def say_clock(language: str | None, hour: int, minute: int) -> str:
    """An hour with the word that goes before it: *alle 7 e 30*, *a mezzanotte*, *all'una*, *at 7:30
    AM*, *at noon*."""
    if language_of(language) == "it":
        if (hour, minute) == (0, 0):
            return "a mezzanotte"
        if (hour, minute) == (12, 0):
            return "a mezzogiorno"
        start = "all'una" if hour == 1 else f"alle {hour}"
        return f"{start} e {minute}" if minute else start
    if (hour, minute) == (0, 0):
        return "at midnight"
    if (hour, minute) == (12, 0):
        return "at noon"
    clock = f"{hour % 12 or 12}:{minute:02d}" if minute else f"{hour % 12 or 12}"
    return f"at {clock} {'AM' if hour < 12 else 'PM'}"


def say_when(language: str | None, at: datetime, now: datetime) -> str:
    """A moment, as it is said from where ``now`` is: *oggi alle 19*, *domani alle 7 e 30*, *lunedì
    alle 8*, *il 19 ottobre alle 7*, *tomorrow at 7:30 AM*, *Monday at 8 AM*. It goes after a *for*
    or a *per*: *Alarm set for Monday at 8 AM*."""
    spoken = language_of(language)
    clock = say_clock(spoken, at.hour, at.minute)
    days = (at.date() - now.date()).days
    weekday = _WEEKDAYS[spoken][at.weekday()]
    month = _MONTHS[spoken][at.month - 1]
    if spoken == "it":
        if days == 0:
            return f"oggi {clock}"
        if days == 1:
            return f"domani {clock}"
        if days == 2:
            return f"dopodomani {clock}"
        if 2 < days < 7:
            return f"{weekday} {clock}"
        return f"il {at.day} {month} {clock}"
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"tomorrow {clock}"
    if days == 2:
        return f"the day after tomorrow {clock}"
    if 2 < days < 7:
        return f"{weekday} {clock}"
    return f"{month} {at.day} {clock}"


def say_list(items: Sequence[str]) -> str:
    """Things said one after the other."""
    return ", ".join(items)
