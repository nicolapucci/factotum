from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from factotum.contracts.conversation import Period
from factotum.conversation.periods import bounds

ROME = ZoneInfo("Europe/Rome")
MONDAY = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)  # 11:30 in Rome


def utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


class TestTheDaysWeeksAndMonthsOfSomebody:
    @pytest.mark.parametrize(
        ("period", "start", "end"),
        [
            (Period.TODAY, utc(2026, 9, 20, 22), utc(2026, 9, 21, 22)),
            (Period.YESTERDAY, utc(2026, 9, 19, 22), utc(2026, 9, 20, 22)),
            (Period.THIS_WEEK, utc(2026, 9, 20, 22), utc(2026, 9, 27, 22)),
            (Period.LAST_WEEK, utc(2026, 9, 13, 22), utc(2026, 9, 20, 22)),
            (Period.THIS_MONTH, utc(2026, 8, 31, 22), utc(2026, 9, 30, 22)),
            (Period.LAST_MONTH, utc(2026, 7, 31, 22), utc(2026, 8, 31, 22)),
        ],
    )
    def test_each_period_in_rome_on_a_monday_morning(
        self, period: Period, start: datetime, end: datetime
    ) -> None:
        assert bounds(period, MONDAY, ROME) == (start, end)

    def test_a_week_begins_on_monday_whatever_the_day_it_is_asked(self) -> None:
        sunday = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

        assert bounds(Period.THIS_WEEK, sunday, UTC) == (utc(2026, 9, 21), utc(2026, 9, 28))

    def test_today_is_the_day_where_the_person_is_and_not_the_one_in_utc(self) -> None:
        just_after_midnight_in_rome = datetime(2026, 9, 21, 22, 30, tzinfo=UTC)

        assert bounds(Period.TODAY, just_after_midnight_in_rome, ROME)[0] == utc(2026, 9, 21, 22)
        assert bounds(Period.TODAY, just_after_midnight_in_rome, UTC)[0] == utc(2026, 9, 21)

    def test_last_month_of_january_is_december_of_the_year_before(self) -> None:
        january = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)

        assert bounds(Period.LAST_MONTH, january, UTC) == (utc(2026, 12, 1), utc(2027, 1, 1))

    def test_this_month_of_december_ends_in_january(self) -> None:
        december = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)

        assert bounds(Period.THIS_MONTH, december, UTC) == (utc(2026, 12, 1), utc(2027, 1, 1))

    def test_the_day_the_clocks_go_back_lasts_twenty_five_hours(self) -> None:
        after = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)  # the clocks went back on the 25th

        start, end = bounds(Period.YESTERDAY, after, ROME)

        assert (end - start).total_seconds() / 3600 == 25

    def test_a_period_ends_where_the_next_begins(self) -> None:
        yesterday = bounds(Period.YESTERDAY, MONDAY, ROME)
        today = bounds(Period.TODAY, MONDAY, ROME)

        assert yesterday[1] == today[0]
