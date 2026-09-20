"""When "yesterday" or "last week" was, for whoever said it, in their own time zone.

A message names a day, a week or a month in the words of a person: "yesterday" is the day before
today *where they are*, which is not the day before in UTC for someone who talks to the assistant
just after midnight. The answer is a half-open range ``[start, end)`` in UTC, so that it can be put
straight into a query.
"""

from datetime import UTC, date, datetime, time, timedelta, tzinfo

from factotum.contracts.conversation import Period


def _start_of(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def _first_of_next_month(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def _first_of_last_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def bounds(period: Period, now: datetime, zone: tzinfo) -> tuple[datetime, datetime]:
    """The start (included) and the end (not) of ``period``, where ``now`` is in ``zone``.
    Weeks begin on Monday."""
    today = now.astimezone(zone).date()
    monday = today - timedelta(days=today.weekday())
    match period:
        case Period.TODAY:
            first, after = today, today + timedelta(days=1)
        case Period.YESTERDAY:
            first, after = today - timedelta(days=1), today
        case Period.THIS_WEEK:
            first, after = monday, monday + timedelta(days=7)
        case Period.LAST_WEEK:
            first, after = monday - timedelta(days=7), monday
        case Period.THIS_MONTH:
            first, after = today.replace(day=1), _first_of_next_month(today)
        case Period.LAST_MONTH:
            first, after = _first_of_last_month(today), today.replace(day=1)
    return _start_of(first, zone), _start_of(after, zone)
