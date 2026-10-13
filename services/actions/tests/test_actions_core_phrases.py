"""What the skills of the platform say of what they did (ADR 0029): the sentences, in each language,
and the words for a length of time, an hour and a day."""

import string
from datetime import datetime

import pytest

from factotum.actions.core.phrases import (
    _CATALOG,
    LANGUAGES,
    CoreSay,
    core_say,
    say_clock,
    say_duration,
    say_list,
    say_when,
)

EVERY_FIELD: dict[str, str | int] = {
    "level": 50,
    "duration": "5 minutes",
    "left": "4 minutes",
    "count": 2,
    "when": "tomorrow at 7",
    "at": "at 7",
    "list": "a, b",
}
"""Every blank that a sentence has, filled: a test of all of them at once."""


def fields_of(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def civil(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001 - civil time


NOW = civil(2026, 10, 13, 22, 1)
"""A Tuesday evening."""


class TestTheCatalog:
    def test_english_and_italian_are_here(self) -> None:
        assert {"en", "it"} == LANGUAGES

    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_every_language_has_every_sentence(self, language: str) -> None:
        assert set(_CATALOG[language]) == set(CoreSay)

    @pytest.mark.parametrize("key", list(CoreSay))
    def test_a_sentence_has_the_same_blanks_in_every_language(self, key: CoreSay) -> None:
        blanks = {language: fields_of(_CATALOG[language][key]) for language in LANGUAGES}

        assert len({frozenset(names) for names in blanks.values()}) == 1, blanks
        assert blanks["en"] <= set(EVERY_FIELD), blanks

    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_every_sentence_is_said_with_its_blanks_filled(self, language: str) -> None:
        for key in CoreSay:
            said = core_say(language, key, **EVERY_FIELD)

            assert said.strip(), (language, key)
            assert "{" not in said, (language, key)

    def test_a_language_there_are_no_words_for_is_answered_in_english(self) -> None:
        assert core_say("fr", CoreSay.TIMERS_NONE) == core_say("en", CoreSay.TIMERS_NONE)
        assert core_say(None, CoreSay.TIMERS_NONE) == "There are no timers."

    def test_what_a_sentence_names_is_in_it(self) -> None:
        for language in LANGUAGES:
            assert "5 minutes" in core_say(language, CoreSay.TIMER_SET, duration="5 minutes")
            assert "tomorrow at 7" in core_say(language, CoreSay.ALARM_SET, when="tomorrow at 7")
            assert "73" in core_say(language, CoreSay.VOLUME_AT, level=73)


class TestSayingALength:
    @pytest.mark.parametrize(
        ("seconds", "english", "italian"),
        [
            (1, "1 second", "1 secondo"),
            (30, "30 seconds", "30 secondi"),
            (60, "1 minute", "1 minuto"),
            (300, "5 minutes", "5 minuti"),
            (3600, "1 hour", "1 ora"),
            (7200, "2 hours", "2 ore"),
            (5400, "1 hour and 30 minutes", "1 ora e 30 minuti"),
            (90, "1 minute and 30 seconds", "1 minuto e 30 secondi"),
            (3910, "1 hour, 5 minutes and 10 seconds", "1 ora, 5 minuti e 10 secondi"),
            (86_400, "24 hours", "24 ore"),
        ],
    )
    def test_it_is_in_digits_and_the_units_of_each_language(
        self, seconds: int, english: str, italian: str
    ) -> None:
        assert say_duration("en", seconds) == english
        assert say_duration("it", seconds) == italian

    def test_no_time_at_all_is_zero_seconds(self) -> None:
        assert say_duration("en", 0) == "0 seconds"

    def test_a_language_there_are_no_words_for_is_said_in_english(self) -> None:
        assert say_duration("fr", 300) == say_duration("en", 300)
        assert say_duration(None, 300) == "5 minutes"


class TestSayingAnHour:
    @pytest.mark.parametrize(
        ("hour", "minute", "english", "italian"),
        [
            (7, 0, "at 7 AM", "alle 7"),
            (7, 30, "at 7:30 AM", "alle 7 e 30"),
            (19, 5, "at 7:05 PM", "alle 19 e 5"),
            (13, 0, "at 1 PM", "alle 13"),
            (1, 0, "at 1 AM", "all'una"),
            (1, 30, "at 1:30 AM", "all'una e 30"),
            (0, 0, "at midnight", "a mezzanotte"),
            (12, 0, "at noon", "a mezzogiorno"),
            (0, 30, "at 12:30 AM", "alle 0 e 30"),
            (12, 30, "at 12:30 PM", "alle 12 e 30"),
            (23, 59, "at 11:59 PM", "alle 23 e 59"),
        ],
    )
    def test_it_goes_with_the_word_that_comes_before_it(
        self, hour: int, minute: int, english: str, italian: str
    ) -> None:
        assert say_clock("en", hour, minute) == english
        assert say_clock("it", hour, minute) == italian


class TestSayingAMoment:
    """From where the device says it is: its today is what a day is counted from."""

    @pytest.mark.parametrize(
        ("moment", "english", "italian"),
        [
            (civil(2026, 10, 13, 23, 30), "today at 11:30 PM", "oggi alle 23 e 30"),
            (civil(2026, 10, 14, 7, 0), "tomorrow at 7 AM", "domani alle 7"),
            (
                civil(2026, 10, 15, 7, 0),
                "the day after tomorrow at 7 AM",
                "dopodomani alle 7",
            ),
            (civil(2026, 10, 16, 8, 15), "Friday at 8:15 AM", "venerdì alle 8 e 15"),
            (civil(2026, 10, 19, 7, 0), "Monday at 7 AM", "lunedì alle 7"),
            (civil(2026, 10, 20, 7, 0), "October 20 at 7 AM", "il 20 ottobre alle 7"),
            (civil(2027, 1, 1, 0, 0), "January 1 at midnight", "il 1 gennaio a mezzanotte"),
        ],
    )
    def test_it_is_a_day_that_a_person_would_say_and_an_hour(
        self, moment: datetime, english: str, italian: str
    ) -> None:
        assert say_when("en", moment, NOW) == english
        assert say_when("it", moment, NOW) == italian

    def test_a_week_ahead_is_a_date_because_the_name_of_the_day_would_be_today_s(self) -> None:
        assert say_when("en", civil(2026, 10, 20, 7, 0), NOW).startswith("October")

    def test_it_goes_by_the_day_on_the_wall_and_not_by_the_hours_between(self) -> None:
        """Ten minutes past midnight is tomorrow even though it is less than a day from now."""
        assert say_when("en", civil(2026, 10, 14, 0, 10), NOW) == "tomorrow at 12:10 AM"


class TestSayingThings:
    def test_one_after_the_other(self) -> None:
        assert say_list(["a", "b", "c"]) == "a, b, c"
        assert say_list(["a"]) == "a"
        assert say_list([]) == ""
