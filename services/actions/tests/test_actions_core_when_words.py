"""The readers of time over words that are already split (ADR 0030): the hour, the day (with the
year of a date) and the length of time that a piece of a message says, for the check that looks for
what a person said in the windows of what they said."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.core.when import (
    SaidDay,
    parse_alarm,
    parse_duration,
    read_day,
    read_duration,
    read_time,
    tokenize,
)
from factotum.contracts.commands import Weekday


def hour_of(said: str) -> tuple[int, int] | None:
    return read_time(tokenize(said, decimals=False))


def day_of(said: str) -> SaidDay | None:
    return read_day(tokenize(said, decimals=False))


class TestTheWords:
    def test_a_number_with_a_decimal_point_is_one_word_in_a_length_and_two_in_an_hour(self) -> None:
        assert tokenize("1,5 hours", decimals=True) == ["1,5", "hours"]
        assert tokenize("7.30", decimals=False) == ["7", "30"]

    def test_accents_are_taken_off_and_the_case_is_not_kept(self) -> None:
        assert tokenize("Lunedì ALLE Sette", decimals=False) == ["lunedi", "alle", "sette"]

    def test_a_length_is_read_the_same_from_the_text_and_from_its_words(self) -> None:
        for said in ("five minutes", "un'ora e mezza", "2h30", "tra dieci minuti", "ciao"):
            assert read_duration(tokenize(said, decimals=True)) == parse_duration(said)


class TestTheHour:
    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("alle sette e mezza", (7, 30)),
            ("at half past seven", (7, 30)),
            ("7:30", (7, 30)),
            ("07:30", (7, 30)),
            ("7 e 30", (7, 30)),
            ("alle sei", (6, 0)),
            ("sette meno un quarto", (6, 45)),
            ("a quarter to eight", (7, 45)),
            ("mezzogiorno", (12, 0)),
            ("midnight", (0, 0)),
            ("7 pm", (19, 0)),
            ("7:30 am", (7, 30)),
            ("alle sette di sera", (19, 0)),
            ("stasera alle sette", (19, 0)),
            ("alle sette di mattina", (7, 0)),
            ("19:00", (19, 0)),
        ],
    )
    def test_it_is_read_on_the_24_hour_clock(self, said: str, expected: tuple[int, int]) -> None:
        assert hour_of(said) == expected

    @pytest.mark.parametrize(
        "said",
        [
            "domani alle sette e mezza",
            "lunedì alle 7:30",
            "alle sette e mezza il 16 ottobre",
            "tomorrow at half past seven",
        ],
    )
    def test_the_day_that_goes_with_it_is_let_be(self, said: str) -> None:
        assert hour_of(said) == (7, 30)

    @pytest.mark.parametrize(
        "said",
        [
            "domani",
            "lunedì",
            "il 16 ottobre",
            "il 16 ottobre 2026",  # a year is not an hour
            "alle sette e alle otto",  # two hours are not one
            "tra dieci minuti",  # a length from now is not an hour
            "alle sette e dopo",
            "sveglia",
            "",
            "25:00",
            "7 am pm",
        ],
    )
    def test_what_says_no_hour_or_says_more_than_one_is_not_read(self, said: str) -> None:
        assert hour_of(said) is None

    @given(st.integers(0, 23), st.integers(0, 59))
    def test_an_hour_written_with_digits_is_read_as_the_same_hour(
        self, hour: int, minute: int
    ) -> None:
        assert hour_of(f"{hour}:{minute:02d}") == (hour, minute)
        assert hour_of(f"{hour:02d}:{minute:02d}") == (hour, minute)


class TestTheDay:
    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("oggi", SaidDay(in_days=0)),
            ("domani", SaidDay(in_days=1)),
            ("tomorrow", SaidDay(in_days=1)),
            ("dopodomani", SaidDay(in_days=2)),
            ("the day after tomorrow", SaidDay(in_days=2)),
            ("tra tre giorni", SaidDay(in_days=3)),
            ("in 1 day", SaidDay(in_days=1)),
            ("tra una settimana", SaidDay(in_days=7)),
            ("stasera", SaidDay(in_days=0)),
            ("domattina", SaidDay(in_days=1)),
            ("lunedì", SaidDay(weekday=Weekday.MONDAY)),
            ("lunedì prossimo", SaidDay(weekday=Weekday.MONDAY)),
            ("on friday", SaidDay(weekday=Weekday.FRIDAY)),
            ("il 15", SaidDay(day=15)),
            ("il 16 ottobre", SaidDay(day=16, month=10)),
            ("16 ottobre", SaidDay(day=16, month=10)),
            ("october 16th", SaidDay(day=16, month=10)),
            ("il primo maggio", SaidDay(day=1, month=5)),
        ],
    )
    def test_it_is_read_as_it_was_said(self, said: str, expected: SaidDay) -> None:
        assert day_of(said) == expected

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("il 16 ottobre 2026", SaidDay(day=16, month=10, year=2026)),
            ("16 ottobre 2026", SaidDay(day=16, month=10, year=2026)),
            ("october 16 2026", SaidDay(day=16, month=10, year=2026)),
            ("il 1 maggio 2027", SaidDay(day=1, month=5, year=2027)),
            ("il 16 ottobre 2026 alle dieci", SaidDay(day=16, month=10, year=2026)),
            ("alle dieci del 16 ottobre 2026", SaidDay(day=16, month=10, year=2026)),
        ],
    )
    def test_the_year_of_a_date_is_read_when_it_is_said(self, said: str, expected: SaidDay) -> None:
        assert day_of(said) == expected

    @pytest.mark.parametrize(
        "said",
        [
            "il 16 ottobre 26",  # two digits are an hour, not a year
            "il 16 ottobre 1800",
            "il 16 ottobre 3000",
            "il 16 ottobre 02026",  # four digits, and not with a zero in front
            "il 15 2026",  # a year follows a month
            "domani 2026",
        ],
    )
    def test_a_year_is_four_digits_after_a_month_and_nothing_else_is_one(self, said: str) -> None:
        assert day_of(said) is None

    @pytest.mark.parametrize("said", ["domani alle sei", "lunedì alle 7:30", "alle sei di domani"])
    def test_the_hour_that_goes_with_it_is_let_be(self, said: str) -> None:
        assert day_of(said) is not None

    @pytest.mark.parametrize(
        "said",
        [
            "alle sette e mezza",
            "sveglia",
            "",
            "domani lunedì",  # two days are not one
            "tra dieci minuti",  # a length from now is not a day
            "domani e dopodomani",
            "il 32 ottobre",
        ],
    )
    def test_what_says_no_day_or_says_more_than_one_is_not_read(self, said: str) -> None:
        assert day_of(said) is None

    @given(st.integers(0, 366))
    def test_a_number_of_days_is_read_as_the_same_number(self, days: int) -> None:
        assert day_of(f"tra {days} giorni") == SaidDay(in_days=days)


class TestTheAlarmIsReadAsItWas:
    """The check of ADR 0030 uses the readers over words; the alarm of ADR 0029 is read the way it
    was, a year or no year."""

    def test_an_alarm_with_a_year_is_still_not_an_alarm(self) -> None:
        assert parse_alarm("alle sette il 16 ottobre 2026") is None

    def test_an_alarm_with_a_date_and_no_year_is_read_as_before(self) -> None:
        reading = parse_alarm("alle sette il 16 ottobre")

        assert reading is not None
        assert (reading.hour, reading.minute) == (7, 0)
        assert (reading.day.day, reading.day.month) == (16, 10)
