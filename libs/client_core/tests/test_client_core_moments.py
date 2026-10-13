"""When an alarm rings: a way of saying a day, worked out against a calendar (ADR 0029)."""

from datetime import date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.client_core.moments import WEEKDAYS, next_moment
from factotum.contracts.commands import AlarmDay, Weekday


def civil(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001 - civil time


NOW = civil(2026, 10, 13, 22, 1, 14)
"""A Tuesday evening."""


def at(hour: int, minute: int, day: AlarmDay | None = None, now: datetime = NOW) -> object:
    return next_moment(now, hour, minute, day or AlarmDay())


class TestNoDayIsTheNextTimeTheHourComes:
    def test_an_hour_that_is_still_to_come_is_today(self) -> None:
        assert at(23, 30) == civil(2026, 10, 13, 23, 30)

    def test_an_hour_that_has_gone_by_is_tomorrow(self) -> None:
        assert at(7, 0) == civil(2026, 10, 14, 7, 0)

    def test_the_minute_that_is_now_has_gone_by_when_the_seconds_have(self) -> None:
        assert at(22, 1) == civil(2026, 10, 14, 22, 1)
        assert at(22, 2) == civil(2026, 10, 13, 22, 2)

    def test_the_very_moment_is_not_in_the_future(self) -> None:
        assert at(22, 1, now=civil(2026, 10, 13, 22, 1, 0)) == civil(2026, 10, 14, 22, 1)

    def test_midnight_is_the_end_of_this_day_and_not_the_beginning_of_it(self) -> None:
        assert at(0, 0) == civil(2026, 10, 14, 0, 0)

    def test_the_end_of_a_month_and_of_a_year_are_not_a_problem(self) -> None:
        late = civil(2026, 12, 31, 23, 30)

        assert at(7, 0, now=late) == civil(2027, 1, 1, 7, 0)
        assert at(7, 0, now=civil(2026, 2, 28, 12)) == civil(2026, 3, 1, 7, 0)
        assert at(7, 0, now=civil(2028, 2, 28, 12)) == civil(2028, 2, 29, 7, 0)


class TestANumberOfDaysAhead:
    def test_today_is_today_if_the_hour_has_not_come(self) -> None:
        assert at(23, 59, AlarmDay(in_days=0)) == civil(2026, 10, 13, 23, 59)

    def test_today_is_over_when_the_hour_has_gone_by(self) -> None:
        assert at(7, 0, AlarmDay(in_days=0)) == "past"

    def test_tomorrow_and_the_day_after(self) -> None:
        assert at(7, 0, AlarmDay(in_days=1)) == civil(2026, 10, 14, 7, 0)
        assert at(7, 0, AlarmDay(in_days=2)) == civil(2026, 10, 15, 7, 0)

    def test_a_few_days_ahead_goes_over_the_end_of_the_month(self) -> None:
        assert at(7, 0, AlarmDay(in_days=20)) == civil(2026, 11, 2, 7, 0)

    def test_a_day_ahead_is_never_over_even_if_the_hour_went_by_today(self) -> None:
        assert at(0, 0, AlarmDay(in_days=1)) == civil(2026, 10, 14, 0, 0)


class TestAWeekday:
    def test_it_is_the_next_such_day(self) -> None:
        assert at(7, 0, AlarmDay(weekday=Weekday.WEDNESDAY)) == civil(2026, 10, 14, 7, 0)
        assert at(7, 0, AlarmDay(weekday=Weekday.MONDAY)) == civil(2026, 10, 19, 7, 0)
        assert at(7, 0, AlarmDay(weekday=Weekday.SUNDAY)) == civil(2026, 10, 18, 7, 0)

    def test_it_is_today_if_today_is_that_day_and_the_hour_has_not_come(self) -> None:
        assert at(23, 0, AlarmDay(weekday=Weekday.TUESDAY)) == civil(2026, 10, 13, 23, 0)

    def test_it_is_a_week_ahead_if_today_is_that_day_and_the_hour_has_gone_by(self) -> None:
        assert at(7, 0, AlarmDay(weekday=Weekday.TUESDAY)) == civil(2026, 10, 20, 7, 0)

    def test_the_calendar_starts_its_week_on_monday(self) -> None:
        assert WEEKDAYS[0] is Weekday.MONDAY
        assert [civil(2026, 10, 12 + n).weekday() for n in range(7)] == list(range(7))


class TestADateOfTheCalendar:
    def test_a_day_of_the_month_is_the_next_time_it_comes(self) -> None:
        assert at(7, 0, AlarmDay(day=15)) == civil(2026, 10, 15, 7, 0)
        assert at(7, 0, AlarmDay(day=13)) == civil(2026, 11, 13, 7, 0)  # today's, gone by
        assert at(23, 0, AlarmDay(day=13)) == civil(2026, 10, 13, 23, 0)

    def test_a_day_that_some_months_do_not_have_waits_for_one_that_does(self) -> None:
        assert at(7, 0, AlarmDay(day=31)) == civil(2026, 10, 31, 7, 0)
        assert at(7, 0, AlarmDay(day=31), now=civil(2026, 11, 1)) == civil(2026, 12, 31, 7, 0)
        assert at(7, 0, AlarmDay(day=30), now=civil(2026, 1, 31, 12)) == civil(2026, 3, 30, 7, 0)

    def test_a_date_with_its_month_is_the_next_time_it_comes(self) -> None:
        assert at(7, 0, AlarmDay(day=15, month=10)) == civil(2026, 10, 15, 7, 0)
        assert at(7, 0, AlarmDay(day=12, month=10)) == civil(2027, 10, 12, 7, 0)
        assert at(7, 0, AlarmDay(day=1, month=1)) == civil(2027, 1, 1, 7, 0)
        assert at(23, 0, AlarmDay(day=13, month=10)) == civil(2026, 10, 13, 23, 0)

    def test_the_twenty_ninth_of_february_waits_for_a_leap_year(self) -> None:
        assert at(7, 0, AlarmDay(day=29, month=2)) == civil(2028, 2, 29, 7, 0)
        assert at(7, 0, AlarmDay(day=29, month=2), now=civil(2029, 3, 1)) == civil(
            2032, 2, 29, 7, 0
        )

    def test_a_day_of_the_month_goes_over_the_end_of_the_year(self) -> None:
        assert at(7, 0, AlarmDay(day=5), now=civil(2026, 12, 10)) == civil(2027, 1, 5, 7, 0)
        assert at(7, 0, AlarmDay(day=31), now=civil(2026, 12, 31, 8)) == civil(2027, 1, 31, 7, 0)
        assert at(7, 0, AlarmDay(day=15), now=civil(2026, 11, 20)) == civil(2026, 12, 15, 7, 0)

    @pytest.mark.parametrize(("day", "month"), [(31, 4), (30, 2), (31, 6), (31, 9), (31, 11)])
    def test_a_date_that_never_comes_is_not_one(self, day: int, month: int) -> None:
        assert at(7, 0, AlarmDay(day=day, month=month)) == "no_such_day"


nows = st.datetimes(min_value=civil(2020, 1, 1), max_value=civil(2036, 12, 31))
hours = st.integers(0, 23)
minutes = st.integers(0, 59)


class TestWhateverTheClockAndTheWords:
    @given(nows, hours, minutes)
    def test_no_day_is_the_first_time_after_now_and_within_a_day(
        self, now: datetime, hour: int, minute: int
    ) -> None:
        moment = next_moment(now, hour, minute, AlarmDay())

        assert isinstance(moment, datetime)
        assert now < moment <= now + timedelta(days=1)
        assert (moment.hour, moment.minute, moment.second) == (hour, minute, 0)

    @given(nows, hours, minutes, st.sampled_from(WEEKDAYS))
    def test_a_weekday_is_the_first_such_day_after_now_and_within_a_week(
        self, now: datetime, hour: int, minute: int, weekday: Weekday
    ) -> None:
        moment = next_moment(now, hour, minute, AlarmDay(weekday=weekday))

        assert isinstance(moment, datetime)
        assert WEEKDAYS[moment.weekday()] is weekday
        assert now < moment <= now + timedelta(days=7)
        assert (moment.hour, moment.minute) == (hour, minute)

    @given(nows, hours, minutes, st.integers(0, 366))
    def test_a_number_of_days_is_that_day_or_it_is_past(
        self, now: datetime, hour: int, minute: int, days: int
    ) -> None:
        moment = next_moment(now, hour, minute, AlarmDay(in_days=days))

        if isinstance(moment, datetime):
            assert moment > now
            assert moment.date() == now.date() + timedelta(days=days)
        else:
            assert moment == "past"
            assert days == 0
            assert datetime.combine(now.date(), civil(2000, 1, 1, hour, minute).time()) <= now

    @given(nows, hours, minutes, st.integers(1, 31), st.one_of(st.none(), st.integers(1, 12)))
    def test_a_date_is_the_first_time_it_comes_after_now(
        self, now: datetime, hour: int, minute: int, number: int, month: int | None
    ) -> None:
        moment = next_moment(now, hour, minute, AlarmDay(day=number, month=month))

        if not isinstance(moment, datetime):
            assert moment == "no_such_day"
            assert month is not None  # every day of the month comes within a year
            assert not any(_exists(year, month, number) for year in range(2020, 2050))
            return
        assert moment > now
        assert moment.day == number
        assert month is None or moment.month == month
        assert (moment.hour, moment.minute) == (hour, minute)
        earlier = [
            candidate
            for candidate in _every_day(now.date(), moment.date())
            if candidate.day == number
            and (month is None or candidate.month == month)
            and datetime.combine(candidate, moment.time()) > now
            and candidate < moment.date()
        ]
        assert earlier == []


def _exists(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _every_day(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]
