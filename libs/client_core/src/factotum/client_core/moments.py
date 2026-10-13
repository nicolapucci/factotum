"""When an alarm rings: a way of saying a day, and an hour, worked out against the calendar of the
place the device is in (ADR 0029).

The platform says *what* a person said (``AlarmDay``: tomorrow, Monday, the 15th of October) and has
no calendar and no clock to turn it into a date; the device has both, and this is the one place that
does it. Everything here is *civil time*, what the wall of the place shows, with no zone: an alarm
for seven is for seven on the wall whatever the clock did in between (summer time, a journey).
"""

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Literal

from factotum.contracts.commands import AlarmDay, Weekday

WEEKDAYS = tuple(Weekday)
"""The days of the week in the order of the calendar: ``date.weekday()`` counts them the same
way."""

_YEARS_AHEAD = 9
"""How far a date with a month is looked for: a 29th of February comes within eight years, and
a date that is not found within nine never comes."""

_MONTHS_AHEAD = 24
"""How far a day of the month with no month is looked for: every day from the 1st to the 31st comes
within a year."""

Unreachable = Literal["past", "no_such_day"]
"""Why there is no moment: it has gone by (*today at six* at noon), or the date never comes
(the 31st of April)."""


def next_moment(now: datetime, hour: int, minute: int, day: AlarmDay) -> datetime | Unreachable:
    """The first moment after ``now`` that is ``hour`` and ``minute`` on a day that ``day`` says, or
    why there is none.

    No day is the next time the hour comes, today or tomorrow. A number of days ahead is that day
    and no other (so *today* can be over). A weekday is the next such day, today if the hour has not
    come yet. A day of the month, with or without its month, is the next time that date comes."""
    at = time(hour, minute)

    def on(when: date) -> datetime:
        return datetime.combine(when, at)

    today = now.date()
    if day.in_days is not None:
        moment = on(today + timedelta(days=day.in_days))
        return moment if moment > now else "past"
    if day.weekday is not None:
        ahead = (WEEKDAYS.index(day.weekday) - today.weekday()) % 7
        moment = on(today + timedelta(days=ahead))
        return moment if moment > now else on(today + timedelta(days=ahead + 7))
    if day.day is not None:
        return _next_date(now, on, day.day, day.month)
    moment = on(today)
    return moment if moment > now else on(today + timedelta(days=1))


def _next_date(
    now: datetime, on: Callable[[date], datetime], number: int, month: int | None
) -> datetime | Unreachable:
    """The next time that the ``number``-th of ``month`` (of any month, when there is none) comes
    after ``now``, at the hour that ``on`` puts on a date."""
    if month is not None:
        candidates = (
            _date(year, month, number) for year in range(now.year, now.year + _YEARS_AHEAD)
        )
    else:
        candidates = (
            _date(
                now.year + (now.month - 1 + ahead) // 12, (now.month - 1 + ahead) % 12 + 1, number
            )
            for ahead in range(_MONTHS_AHEAD)
        )
    for candidate in candidates:
        if candidate is not None and on(candidate) > now:
            return on(candidate)
    return "no_such_day"


def _date(year: int, month: int, number: int) -> date | None:
    """That date, or nothing when the month has no such day that year."""
    try:
        return date(year, month, number)
    except ValueError:
        return None
