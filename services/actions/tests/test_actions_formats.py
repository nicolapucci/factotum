"""A value said in words, written in the form a skill declares (ADR 0030): what a message says of a
time, a length and a day, how a model's value is read, how each form is written, and what a skill
is given for what a person said."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.core.when import SaidDay
from factotum.actions.formats import (
    Length,
    TimeOfDay,
    convert,
    numeric_dates,
    read_value,
    said_in,
    write,
)
from factotum.contracts.commands import Weekday
from factotum.contracts.formats import FormatGroup, FormatKind, ValueFormat
from factotum.contracts.skills import ParameterSpec, ParamType


def spec(param_type: str, *formats: dict[str, str]) -> ParameterSpec:
    return ParameterSpec.model_validate({"type": param_type, "formats": list(formats)})


def fmt(kind: str, pattern: str) -> ValueFormat:
    return ValueFormat(kind=FormatKind(kind), pattern=pattern)


TIME = spec("string", {"time": "HH:mm"})
DAY = spec("string", {"date": "dd/MM/yy"}, {"in_days": "+{n}d"}, {"weekday": "name"})


class TestWhatAMessageSays:
    def test_an_hour_is_said_where_it_is_said(self) -> None:
        assert said_in("svegliami alle sette e mezza", FormatGroup.TIME) == {TimeOfDay(7, 30)}

    def test_the_inside_of_what_is_said_is_not_said(self) -> None:
        """*Sette* is an hour of itself, but not in *sette e mezza*: the person did not say 7:00."""
        assert TimeOfDay(7, 0) not in said_in("alle sette e mezza", FormatGroup.TIME)
        assert Length(600) not in said_in("per dieci minuti e mezzo", FormatGroup.DURATION)

    def test_two_hours_are_two_readings(self) -> None:
        said = said_in("dalle sette alle otto e un quarto", FormatGroup.TIME)

        assert said == {TimeOfDay(7, 0), TimeOfDay(8, 15)}

    def test_the_period_makes_the_hour_in_the_window_that_has_it(self) -> None:
        said = said_in("alle sette e mezza di sera", FormatGroup.TIME)

        assert said == {TimeOfDay(19, 30)}

    def test_a_day_that_goes_with_an_hour_is_a_day_of_its_own(self) -> None:
        message = "domani alle sette e mezza"

        assert said_in(message, FormatGroup.DAY) == {SaidDay(in_days=1)}
        assert said_in(message, FormatGroup.TIME) == {TimeOfDay(7, 30)}

    def test_a_length_is_not_an_hour_and_an_hour_is_not_a_length(self) -> None:
        assert said_in("per due ore e mezza", FormatGroup.DURATION) == {Length(9000)}
        assert said_in("tra dieci minuti", FormatGroup.TIME) == set()
        assert said_in("alle sette e mezza", FormatGroup.DURATION) == set()

    def test_a_date_with_its_year_is_one_day(self) -> None:
        said = said_in("il 16 ottobre 2026 alle dieci", FormatGroup.DAY)

        assert said == {SaidDay(day=16, month=10, year=2026)}

    def test_a_date_in_digits_is_a_day_and_not_an_hour_and_a_year(self) -> None:
        message = "il 16/10/26 alle dieci"

        assert said_in(message, FormatGroup.DAY) == {SaidDay(day=16, month=10, year=2026)}
        assert said_in(message, FormatGroup.TIME) == {TimeOfDay(10, 0)}

    def test_two_dates_in_digits_are_two_days_and_neither_is_an_hour(self) -> None:
        message = "dal 16/10/2026 al 20/10/26 alle dieci"

        assert said_in(message, FormatGroup.DAY) == {
            SaidDay(day=16, month=10, year=2026),
            SaidDay(day=20, month=10, year=2026),
        }
        assert said_in(message, FormatGroup.TIME) == {TimeOfDay(10, 0)}
        assert said_in(message, FormatGroup.DURATION) == set()

    def test_a_length_with_a_decimal_point_is_one_number(self) -> None:
        assert said_in("registra per 1,5 ore", FormatGroup.DURATION) == {Length(5400)}
        assert said_in("registra per 2.5 minuti", FormatGroup.DURATION) == {Length(150)}

    def test_what_is_said_may_be_longer_than_a_few_words(self) -> None:
        message = "registra per un'ora e trenta minuti e dieci secondi"

        assert said_in(message, FormatGroup.DURATION) == {Length(3600 + 1800 + 10)}

    def test_nothing_said_is_nothing(self) -> None:
        for group in FormatGroup:
            assert said_in("accendi la luce", group) == set()
            assert said_in("", group) == set()

    def test_it_is_looked_for_in_either_language(self) -> None:
        assert said_in("wake me at half past seven", FormatGroup.TIME) == {TimeOfDay(7, 30)}
        assert said_in("for an hour and a half", FormatGroup.DURATION) == {Length(5400)}
        assert said_in("on friday", FormatGroup.DAY) == {SaidDay(weekday=Weekday.FRIDAY)}


class TestNumericDates:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("16/10/26", SaidDay(day=16, month=10, year=2026)),
            ("16/10/2026", SaidDay(day=16, month=10, year=2026)),
            ("16.10.2026", SaidDay(day=16, month=10, year=2026)),
            ("16-10-2026", SaidDay(day=16, month=10, year=2026)),
            ("2026-10-16", SaidDay(day=16, month=10, year=2026)),
            ("1/2/2027", SaidDay(day=1, month=2, year=2027)),
            ("16/10", SaidDay(day=16, month=10)),
            ("29/02/2028", SaidDay(day=29, month=2, year=2028)),
            ("29/02", SaidDay(day=29, month=2)),
        ],
    )
    def test_the_day_comes_first(self, text: str, expected: SaidDay) -> None:
        [(start, end, said)] = numeric_dates(text)

        assert (start, end, said) == (0, len(text), expected)

    @pytest.mark.parametrize(
        "text",
        [
            "7.30",  # an hour
            "10-20",  # two numbers are a date only with a slash
            "10.11",
            "10-11",
            "31/02/2026",  # there is no such day
            "29/02/2027",
            "16/13/2026",
            "32/10/2026",
            "0/10/2026",
            "16/10/1800",  # nor such a year
            "16/10/3000",
            "2026-13-01",
            "1234567",
            "16/10/26/1",  # more than a date
            "116/10/26",
            "no date at all",
        ],
    )
    def test_what_is_not_a_date_is_not_one(self, text: str) -> None:
        assert numeric_dates(text) == []

    def test_where_it_is_found_is_said_with_it(self) -> None:
        text = "il 16/10/26 e il 2026-11-01."

        assert [(start, end) for start, end, _ in numeric_dates(text)] == [(3, 11), (17, 27)]


class TestReadingAValue:
    def test_a_model_writes_the_words_or_the_form_of_them(self) -> None:
        for written in ("alle sette e mezza", "7:30", "07:30", "half past seven", "7.30"):
            assert read_value(written, FormatGroup.TIME) == TimeOfDay(7, 30)

    def test_a_day_may_be_written_in_digits(self) -> None:
        assert read_value("16/10/26", FormatGroup.DAY) == SaidDay(day=16, month=10, year=2026)
        assert read_value(" 2026-10-16 ", FormatGroup.DAY) == SaidDay(day=16, month=10, year=2026)

    def test_a_number_of_days_may_lack_the_word_that_begins_it_in_a_value_and_not_in_a_message(
        self,
    ) -> None:
        assert read_value("three days", FormatGroup.DAY) == SaidDay(in_days=3)
        assert read_value("2 weeks", FormatGroup.DAY) == SaidDay(in_days=14)
        assert said_in("per three days", FormatGroup.DAY) == set()
        assert said_in("stay for three days", FormatGroup.DAY) == set()

    def test_only_a_number_of_days_may_lack_it(self) -> None:
        for written in ("three hours", "days", "three", "monday days", "ten minutes"):
            assert read_value(written, FormatGroup.DAY) is None
        assert read_value("three days", FormatGroup.DURATION) is None
        assert read_value("three days", FormatGroup.TIME) is None

    def test_a_date_in_digits_may_have_an_article_in_front_of_it(self) -> None:
        for written in ("il 16/10/26", "the 16/10/26", "on 16/10/26", "il 16/10/26."):
            assert read_value(written, FormatGroup.DAY) == SaidDay(day=16, month=10, year=2026)

    def test_a_date_in_digits_is_that_date_and_nothing_else(self) -> None:
        assert read_value("16/10/26 alle dieci", FormatGroup.DAY) is None
        assert read_value("dopo 16/10/26", FormatGroup.DAY) is None
        assert read_value("16/10/26 e 17/10/26", FormatGroup.DAY) is None
        assert read_value("16/10/26 17/10/26", FormatGroup.DAY) is None
        assert read_value("16/10/26", FormatGroup.TIME) is None
        assert read_value("16/10/26", FormatGroup.DURATION) is None
        assert read_value("10/11", FormatGroup.TIME) is None  # a date, and not 10:11

    def test_a_day_that_is_not_a_date_in_digits_is_read_as_words(self) -> None:
        assert read_value("domani", FormatGroup.DAY) == SaidDay(in_days=1)

    def test_a_length_has_its_unit_or_it_is_not_one(self) -> None:
        assert read_value("cinque minuti", FormatGroup.DURATION) == Length(300)
        assert read_value("300", FormatGroup.DURATION) is None

    def test_what_is_not_understood_is_nothing(self) -> None:
        for group in FormatGroup:
            assert read_value("la luce della cucina", group) is None


class TestWritingAForm:
    @pytest.mark.parametrize(
        ("pattern", "said", "written"),
        [
            ("HH:mm", TimeOfDay(7, 5), "07:05"),
            ("H.mm", TimeOfDay(7, 5), "7.05"),
            ("HH:mm", TimeOfDay(0, 0), "00:00"),
            ("H:mm", TimeOfDay(23, 59), "23:59"),
            ("hh:mm a", TimeOfDay(7, 30), "07:30 am"),
            ("h:mm a", TimeOfDay(19, 30), "7:30 pm"),
            ("h:mm a", TimeOfDay(0, 5), "12:05 am"),
            ("h:mm a", TimeOfDay(12, 0), "12:00 pm"),
            ("hh:mm a", TimeOfDay(13, 0), "01:00 pm"),
        ],
    )
    def test_a_time(self, pattern: str, said: TimeOfDay, written: str) -> None:
        assert write(fmt("time", pattern), said, ParamType.STRING) == written

    @pytest.mark.parametrize(
        ("pattern", "said", "written"),
        [
            ("dd/MM/yy", SaidDay(day=16, month=10, year=2026), "16/10/26"),
            ("dd/MM/yyyy", SaidDay(day=6, month=1, year=2026), "06/01/2026"),
            ("d/M/yyyy", SaidDay(day=6, month=1, year=2026), "6/1/2026"),
            ("yyyy-MM-dd", SaidDay(day=6, month=1, year=2026), "2026-01-06"),
            ("dd/MM", SaidDay(day=6, month=1), "06/01"),
            ("dd/MM", SaidDay(day=6, month=1, year=2030), "06/01"),
            ("dd/MM/yy", SaidDay(day=6, month=1, year=2005), "06/01/05"),
        ],
    )
    def test_a_date(self, pattern: str, said: SaidDay, written: str) -> None:
        assert write(fmt("date", pattern), said, ParamType.STRING) == written

    @pytest.mark.parametrize(
        "said",
        [
            SaidDay(day=16, month=10),  # no year to write
            SaidDay(day=16),  # no month
            SaidDay(in_days=1),
            SaidDay(weekday=Weekday.MONDAY),
        ],
    )
    def test_a_date_that_cannot_be_written_as_asked_is_not_written(self, said: SaidDay) -> None:
        assert write(fmt("date", "dd/MM/yy"), said, ParamType.STRING) is None

    @pytest.mark.parametrize(
        "said", [SaidDay(day=16), SaidDay(in_days=1), SaidDay(weekday=Weekday.MONDAY), SaidDay()]
    )
    def test_nor_is_a_day_that_has_no_month_for_a_form_that_needs_no_year(
        self, said: SaidDay
    ) -> None:
        assert write(fmt("date", "dd/MM"), said, ParamType.STRING) is None

    def test_the_number_of_days_from_today(self) -> None:
        assert write(fmt("in_days", "+{n}d"), SaidDay(in_days=3), ParamType.STRING) == "+3d"
        assert write(fmt("in_days", "tra {n} giorni"), SaidDay(in_days=0), ParamType.STRING) == (
            "tra 0 giorni"
        )
        assert write(fmt("in_days", "{n}"), SaidDay(in_days=3), ParamType.INTEGER) == 3

    def test_a_day_that_does_not_count_from_today_is_not_a_number_of_days(self) -> None:
        for said in (SaidDay(weekday=Weekday.MONDAY), SaidDay(day=16, month=10)):
            assert write(fmt("in_days", "{n}"), said, ParamType.INTEGER) is None

    def test_the_day_of_the_week(self) -> None:
        said = SaidDay(weekday=Weekday.WEDNESDAY)

        assert write(fmt("weekday", "name"), said, ParamType.STRING) == "wednesday"
        assert write(fmt("weekday", "iso"), said, ParamType.INTEGER) == 3
        assert write(fmt("weekday", "iso"), SaidDay(weekday=Weekday.MONDAY), ParamType.INTEGER) == 1
        assert write(fmt("weekday", "iso"), SaidDay(weekday=Weekday.SUNDAY), ParamType.INTEGER) == 7
        assert write(fmt("weekday", "name"), SaidDay(in_days=1), ParamType.STRING) is None

    @pytest.mark.parametrize(
        ("unit", "param_type", "seconds", "written"),
        [
            ("seconds", ParamType.INTEGER, 300, 300),
            ("minutes", ParamType.INTEGER, 300, 5),
            ("hours", ParamType.INTEGER, 7200, 2),
            ("minutes", ParamType.NUMBER, 90, 1.5),
            ("hours", ParamType.NUMBER, 5400, 1.5),
            ("minutes", ParamType.NUMBER, 300, 5),
            ("iso", ParamType.STRING, 5400, "PT1H30M"),
            ("iso", ParamType.STRING, 300, "PT5M"),
            ("iso", ParamType.STRING, 45, "PT45S"),
            ("iso", ParamType.STRING, 3661, "PT1H1M1S"),
            ("iso", ParamType.STRING, 7200, "PT2H"),
            ("iso", ParamType.STRING, 90000, "PT25H"),
            ("iso", ParamType.STRING, 0, "PT0S"),
        ],
    )
    def test_a_length(self, unit: str, param_type: ParamType, seconds: int, written: Any) -> None:
        assert write(fmt("duration", unit), Length(seconds), param_type) == written

    def test_a_whole_number_of_minutes_is_not_made_of_half_a_minute(self) -> None:
        assert write(fmt("duration", "minutes"), Length(90), ParamType.INTEGER) is None
        assert write(fmt("duration", "hours"), Length(5400), ParamType.INTEGER) is None

    def test_a_form_does_not_write_what_is_of_another_group(self) -> None:
        assert write(fmt("time", "HH:mm"), Length(60), ParamType.STRING) is None
        assert write(fmt("duration", "seconds"), TimeOfDay(7, 0), ParamType.INTEGER) is None
        assert write(fmt("date", "dd/MM/yy"), TimeOfDay(7, 0), ParamType.STRING) is None


class TestWhatASkillIsGiven:
    @pytest.mark.parametrize(
        ("written", "message", "given"),
        [
            ("7:30", "svegliami alle sette e mezza", "07:30"),
            ("sette e mezza", "svegliami alle sette e mezza", "07:30"),
            ("alle sette e mezza", "svegliami alle sette e mezza", "07:30"),
            ("07:30", "svegliami domani alle sette e mezza", "07:30"),
            ("half past seven", "svegliami alle sette e mezza", "07:30"),
            ("19:30", "svegliami alle sette e mezza di sera", "19:30"),
            ("7:30 pm", "svegliami alle sette e mezza di sera", "19:30"),
            ("8:00", "dalle sette alle otto", "08:00"),
            ("7:00", "dalle sette alle otto", "07:00"),
        ],
    )
    def test_a_time_that_was_said_is_written_in_the_form(
        self, written: str, message: str, given: str
    ) -> None:
        assert convert(TIME, written, message) == given

    @pytest.mark.parametrize(
        ("written", "message"),
        [
            ("7:00", "svegliami alle sette e mezza"),  # the half is said
            ("7:30", "svegliami alle sette e mezza di sera"),  # so is the evening
            ("19:30", "svegliami alle sette e mezza"),  # and not said when it is not
            ("9:00", "dalle sette alle otto"),
            ("domani", "svegliami alle sette e mezza"),
            ("seven", "svegliami alle sette e mezza"),  # not the words of the message
            ("25:00", "svegliami alle 25:00"),
            ("", "svegliami alle sette"),
            ("7:30", ""),
        ],
    )
    def test_a_time_that_was_not_said_is_not_given(self, written: str, message: str) -> None:
        assert convert(TIME, written, message) is None

    @pytest.mark.parametrize(
        ("written", "message", "given"),
        [
            ("domani", "metti la riunione domani", "+1d"),
            ("tomorrow", "metti la riunione domani", "+1d"),
            ("tra tre giorni", "metti la riunione fra tre giorni", "+3d"),
            ("lunedì", "metti la riunione lunedì", "monday"),
            ("16/10/2026", "metti la riunione il 16 ottobre 2026", "16/10/26"),
            ("16/10/26", "metti la riunione il 16/10/26", "16/10/26"),
            ("il 16 ottobre 2026", "riunione il 16 ottobre 2026", "16/10/26"),
        ],
    )
    def test_a_day_is_written_in_the_first_form_that_can_say_it(
        self, written: str, message: str, given: str
    ) -> None:
        assert convert(DAY, written, message) == given

    @pytest.mark.parametrize(
        ("written", "message"),
        [
            ("domani", "metti la riunione dopodomani"),
            ("il 16 ottobre", "metti la riunione il 16 ottobre"),  # a date with no year
            ("16/10/2026", "metti la riunione il 16 ottobre"),  # a year nobody said
            ("martedì", "metti la riunione lunedì"),
        ],
    )
    def test_a_day_that_was_not_said_or_cannot_be_written_is_not_given(
        self, written: str, message: str
    ) -> None:
        assert convert(DAY, written, message) is None

    def test_a_day_is_written_by_the_one_form_that_can_say_it_whatever_the_order(self) -> None:
        by_days_first = spec("string", {"in_days": "+{n}d"}, {"weekday": "name"})
        by_name_first = spec("string", {"weekday": "name"}, {"in_days": "+{n}d"})

        for param in (by_days_first, by_name_first):
            assert convert(param, "domani", "riunione domani") == "+1d"
            assert convert(param, "lunedì", "riunione lunedì") == "monday"

    @pytest.mark.parametrize(
        ("param", "written", "message", "given"),
        [
            (spec("integer", {"duration": "minutes"}), "mezz'ora", "registra per mezz'ora", 30),
            (spec("integer", {"duration": "minutes"}), "30 minutes", "registra per mezz'ora", 30),
            (spec("integer", {"duration": "seconds"}), "5 minuti", "timer di cinque minuti", 300),
            (
                spec("string", {"duration": "iso"}),
                "un'ora e mezza",
                "tra un'ora e mezza",
                "PT1H30M",
            ),
        ],
    )
    def test_a_length_is_written_in_its_unit(
        self, param: ParameterSpec, written: str, message: str, given: Any
    ) -> None:
        assert convert(param, written, message) == given

    @pytest.mark.parametrize(
        ("written", "message"),
        [
            ("300", "timer di cinque minuti"),  # a number with no unit is nothing said
            ("10 minuti", "timer di cinque minuti"),
            ("5 minuti", "timer di un'ora e cinque minuti"),
        ],
    )
    def test_a_length_that_was_not_said_is_not_given(self, written: str, message: str) -> None:
        assert convert(spec("integer", {"duration": "seconds"}), written, message) is None

    def test_the_person_said_the_whole_of_what_a_model_copied_only_part_of(self) -> None:
        """The model says where, and the person said what (*alle nove* for *stasera alle nove*)."""
        length = spec("integer", {"duration": "seconds"})

        assert convert(TIME, "alle nove", "stasera alle nove registra il canale 5") == "21:00"
        assert convert(TIME, "sette", "svegliami alle sette e mezza") == "07:30"
        assert convert(TIME, "alle sette", "domani sera alle sette") == "19:00"
        assert convert(length, "un'ora", "timer di un'ora e cinque minuti") == 3900
        assert convert(DAY, "il 16 ottobre", "il 16 ottobre 2026") == "16/10/26"

    def test_what_a_model_copied_from_two_places_that_say_different_things_is_not_a_value(
        self,
    ) -> None:
        message = "alle sette e mezza e poi alle sette di sera"

        assert convert(TIME, "alle sette", message) is None
        assert convert(TIME, "alle sette e mezza", message) == "07:30"

    def test_a_model_that_contradicts_the_person_is_not_pointing_at_what_they_said(self) -> None:
        assert convert(TIME, "sette di mattina", "svegliami alle sette di sera") is None
        assert convert(TIME, "7", "svegliami alle sette e mezza") is None  # not their words
        assert convert(TIME, "sera", "svegliami alle sette di sera") is None  # no hour at all
        assert convert(TIME, "la sera", "svegliami alle sette di sera") is None

    def test_a_day_without_the_word_that_begins_it_is_the_day_it_would_have_been(self) -> None:
        assert convert(DAY, "three days", "metti la riunione in three days") == "+3d"
        assert convert(DAY, "tre giorni", "metti la riunione fra tre giorni") == "+3d"
        assert convert(DAY, "two weeks", "metti la riunione in two weeks") == "+14d"

    def test_a_length_that_the_form_cannot_say_is_not_given(self) -> None:
        whole_minutes = spec("integer", {"duration": "minutes"})

        assert convert(whole_minutes, "novanta secondi", "per novanta secondi") is None

    def test_what_is_not_text_or_a_number_is_not_a_value(self) -> None:
        not_values: tuple[Any, ...] = (None, True, False, [], {}, "", "  ", 7.0, b"7")
        for raw in not_values:
            assert convert(TIME, raw, "alle sette") is None

    def test_a_number_a_model_wrote_is_read_as_the_text_of_it(self) -> None:
        assert convert(TIME, 7, "svegliami alle sette") == "07:00"
        assert convert(TIME, 7.5, "svegliami alle 7.5") is None

    def test_a_spec_with_no_formats_gives_nothing(self) -> None:
        assert convert(ParameterSpec(type=ParamType.STRING), "7:30", "alle sette e mezza") is None

    @given(st.integers(0, 23), st.integers(0, 59))
    def test_an_hour_that_was_said_in_digits_is_given_as_it_was_said(
        self, hour: int, minute: int
    ) -> None:
        said = f"{hour}:{minute:02d}"

        assert convert(TIME, said, f"svegliami alle {said}") == f"{hour:02d}:{minute:02d}"

    @given(st.integers(0, 23), st.integers(0, 59), st.integers(0, 23), st.integers(0, 59))
    def test_an_hour_that_was_not_said_is_never_given(
        self, hour: int, minute: int, other_hour: int, other_minute: int
    ) -> None:
        if (hour, minute) == (other_hour, other_minute):
            return

        written = f"{other_hour}:{other_minute:02d}"
        said = f"svegliami alle {hour}:{minute:02d}"

        assert convert(TIME, written, said) is None

    @given(st.integers(1, 600))
    def test_a_length_that_was_said_is_given_in_its_unit(self, minutes: int) -> None:
        param = spec("integer", {"duration": "seconds"})

        assert convert(param, f"{minutes} minuti", f"registra per {minutes} minuti") == minutes * 60

    @given(st.text(max_size=80), st.text(max_size=120))
    def test_whatever_is_written_and_said_it_is_given_or_it_is_not_and_never_an_error(
        self, written: str, message: str
    ) -> None:
        for param in (TIME, DAY, spec("integer", {"duration": "seconds"})):
            convert(param, written, message)
