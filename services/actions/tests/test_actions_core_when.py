"""What a person says of time, read by code (ADR 0029): how long a timer lasts, and the hour and the
day of an alarm, in English and in Italian."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.core.phrases import say_clock, say_duration
from factotum.actions.core.when import AlarmWords, parse_alarm, parse_duration
from factotum.contracts.commands import AlarmDay, Weekday

NEXT = AlarmDay()
"""No day was said: the next time the hour comes."""


class TestHowLongInEnglish:
    @pytest.mark.parametrize(
        ("said", "seconds"),
        [
            ("five minutes", 300),
            ("5 minutes", 300),
            ("five minute", 300),
            ("90 seconds", 90),
            ("1 hour", 3600),
            ("an hour", 3600),
            ("a minute", 60),
            ("thirty seconds", 30),
            ("an hour and a half", 5400),
            ("one and a half hours", 5400),
            ("two and a half minutes", 150),
            ("half an hour", 1800),
            ("half a minute", 30),
            ("half hour", 1800),
            ("a half hour", 1800),
            ("a quarter of an hour", 900),
            ("three quarters of an hour", 2700),
            ("an hour and a quarter", 4500),
            ("one hour thirty minutes", 5400),
            ("1 hour 30 minutes", 5400),
            ("1 hour and 30 minutes", 5400),
            ("1 hour, 5 minutes and 10 seconds", 3910),
            ("one minute thirty", 90),
            ("2h30", 9000),
            ("2h 30m", 9000),
            ("1h30m", 5400),
            ("45min", 2700),
            ("90s", 90),
            ("1.5 hours", 5400),
            ("2,5 minutes", 150),
            ("twenty five minutes", 1500),
            ("a hundred minutes", 6000),
            ("for five minutes", 300),
            ("five minutes for a", 300),
            ("1.7 seconds", 2),
            ("a 5 minute timer", 300),
            ("FIVE   Minutes", 300),
            ("five more minutes", 300),
            ("two more hours", 7200),
            ("another five minutes", 300),
            ("another hour", 3600),
            ("another half hour", 1800),
            ("5 more minutes", 300),
            ("five minutes more", 300),
        ],
    )
    def test_it_is_read(self, said: str, seconds: int) -> None:
        assert parse_duration(said) == seconds


class TestHowLongInItalian:
    @pytest.mark.parametrize(
        ("said", "seconds"),
        [
            ("cinque minuti", 300),
            ("5 minuti", 300),
            ("ancora cinque minuti", 300),
            ("altri cinque minuti", 300),
            ("cinque minuti ancora", 300),
            ("un altro minuto", 60),
            ("ancora un'ora", 3600),
            ("ancora mezz'ora", 1800),
            ("venti secondi", 20),
            ("centottanta secondi", 180),
            ("novanta minuti", 5400),
            ("un minuto", 60),
            ("un'ora", 3600),
            ("un'ora e mezza", 5400),
            ("un'ora e mezzo", 5400),
            ("un'ora e un quarto", 4500),
            ("un'ora e tre quarti", 6300),
            ("un'ora e 30", 5400),
            ("un'ora e un", 3600),
            ("un'ora e trenta minuti", 5400),
            ("due ore e dieci", 7800),
            ("due ore e mezza", 9000),
            ("due minuti e mezzo", 150),
            ("un minuto e mezzo", 90),
            ("un minuto e trenta", 90),
            ("mezz'ora", 1800),
            ("mezza ora", 1800),
            ("mezzo minuto", 30),
            ("un quarto d'ora", 900),
            ("tre quarti d'ora", 2700),
            ("tra dieci minuti", 600),
            ("fra venti minuti", 1200),
            ("per mezz'ora", 1800),
            ("di cinque minuti", 300),
            ("1,5 ore", 5400),
            ("un timer di cinque minuti", 300),
        ],
    )
    def test_it_is_read(self, said: str, seconds: int) -> None:
        assert parse_duration(said) == seconds

    def test_the_languages_are_read_whichever_the_person_speaks(self) -> None:
        assert parse_duration("un'ora and thirty minuti") == 5400
        assert parse_duration("cinque minutes") == 300


class TestWhatIsNotALength:
    @pytest.mark.parametrize(
        "said",
        [
            "",
            "   ",
            "soon",
            "ten",
            "five",
            "cinque",
            "venticinque",
            "minutes",
            "ore",
            "5 e 30",
            "five minutes tomorrow",
            "five minutes and then some",
            "qualche minuto",
            "a while",
            "five fortnights",
            "half",
            "mezza",
            "and a half",
            "1.5.2 hours",
            "∞ minutes",
            "more",
            "five more",
            "more minutes",
            "another",
            "ancora",
            "altri",
            "ancora cinque",
            "five more things",
        ],
    )
    def test_it_is_not_guessed(self, said: str) -> None:
        assert parse_duration(said) is None

    def test_a_number_alone_after_seconds_is_not_a_smaller_unit(self) -> None:
        assert parse_duration("ten seconds five") is None

    def test_a_length_is_not_checked_for_its_size_here(self) -> None:
        """What a timer may be is the skill's to say (a day at most): this reads what was said."""
        assert parse_duration("cento ore") == 360_000
        assert parse_duration("0 seconds") == 0


class TestLengthsRoundTrip:
    @given(st.integers(1, 86_400), st.sampled_from(["en", "it"]))
    def test_what_is_said_of_a_length_is_read_as_the_same_length(
        self, seconds: int, language: str
    ) -> None:
        assert parse_duration(say_duration(language, seconds)) == seconds

    @given(st.text(max_size=40))
    def test_whatever_is_said_is_read_or_it_is_not_and_never_an_error(self, said: str) -> None:
        result = parse_duration(said)

        assert result is None or isinstance(result, int)


def alarm(hour: int, minute: int = 0, day: AlarmDay = NEXT) -> AlarmWords:
    return AlarmWords(hour, minute, day)


class TestTheHourInItalian:
    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("alle sette", alarm(7)),
            ("sette", alarm(7)),
            ("alle 7", alarm(7)),
            ("alle ore 7", alarm(7)),
            ("alle 7:30", alarm(7, 30)),
            ("alle 7.30", alarm(7, 30)),
            ("alle 7 e 30", alarm(7, 30)),
            ("alle sette e trenta", alarm(7, 30)),
            ("alle sette e trentacinque", alarm(7, 35)),
            ("alle sette e mezza", alarm(7, 30)),
            ("alle sette e mezzo", alarm(7, 30)),
            ("alle sette e un quarto", alarm(7, 15)),
            ("alle sette e quindici", alarm(7, 15)),
            ("alle sette e tre quarti", alarm(7, 45)),
            ("alle sette meno un quarto", alarm(6, 45)),
            ("alle sette meno dieci", alarm(6, 50)),
            ("alle 7 meno 5", alarm(6, 55)),
            ("alle diciannove", alarm(19)),
            ("alle diciannove e trenta", alarm(19, 30)),
            ("alle 18", alarm(18)),
            ("alle 0:30", alarm(0, 30)),
            ("alle ventitré e cinquantanove", alarm(23, 59)),
            ("all'una", alarm(1)),
            ("all'una e mezza", alarm(1, 30)),
            ("a mezzanotte", alarm(0)),
            ("a mezzogiorno", alarm(12)),
            ("alle 6 e 45", alarm(6, 45)),
            ("delle sette", alarm(7)),
            ("delle sette e mezza", alarm(7, 30)),
            ("delle 18", alarm(18)),
            ("dell'una", alarm(1)),
            ("dalle sette", alarm(7)),
            ("dalle 7:30", alarm(7, 30)),
            ("dall'una", alarm(1)),
        ],
    )
    def test_it_is_read(self, said: str, expected: AlarmWords) -> None:
        assert parse_alarm(said) == expected

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("alle sette di sera", alarm(19)),
            ("alle sette di mattina", alarm(7)),
            ("alle sette del mattino", alarm(7)),
            ("alle sette del pomeriggio", alarm(19)),
            ("alle 7 e mezza di sera", alarm(19, 30)),
            ("alle 12 di notte", alarm(0)),
            ("alle 11 di notte", alarm(23)),
            ("alle 3 di notte", alarm(3)),
            ("alle dodici di mattina", alarm(0)),
            ("alle 12 del pomeriggio", alarm(12)),
            ("di sera alle sette", alarm(19)),
        ],
    )
    def test_the_period_of_the_day_makes_the_hour(self, said: str, expected: AlarmWords) -> None:
        assert parse_alarm(said) == expected


class TestTheHourInEnglish:
    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("seven", alarm(7)),
            ("at seven", alarm(7)),
            ("at 7", alarm(7)),
            ("seven o'clock", alarm(7)),
            ("at seven thirty", alarm(7, 30)),
            ("six forty five", alarm(6, 45)),
            ("7:30", alarm(7, 30)),
            ("half past seven", alarm(7, 30)),
            ("quarter past seven", alarm(7, 15)),
            ("a quarter past seven", alarm(7, 15)),
            ("ten past seven", alarm(7, 10)),
            ("ten minutes past seven", alarm(7, 10)),
            ("twenty five past seven", alarm(7, 25)),
            ("a quarter to eight", alarm(7, 45)),
            ("quarter to eight", alarm(7, 45)),
            ("twenty to eight", alarm(7, 40)),
            ("five to one", alarm(0, 55)),
            ("at noon", alarm(12)),
            ("midday", alarm(12)),
            ("at midnight", alarm(0)),
        ],
    )
    def test_it_is_read(self, said: str, expected: AlarmWords) -> None:
        assert parse_alarm(said) == expected

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("7 am", alarm(7)),
            ("7 pm", alarm(19)),
            ("7:30 pm", alarm(19, 30)),
            ("7:30 p.m.", alarm(19, 30)),
            ("7 a.m.", alarm(7)),
            ("12 am", alarm(0)),
            ("12 pm", alarm(12)),
            ("12:30 am", alarm(0, 30)),
            ("half past seven in the evening", alarm(19, 30)),
            ("seven in the morning", alarm(7)),
            ("quarter to eight pm", alarm(19, 45)),
            ("9 at night", alarm(21)),
            ("3 in the afternoon", alarm(15)),
        ],
    )
    def test_the_period_of_the_day_makes_the_hour(self, said: str, expected: AlarmWords) -> None:
        assert parse_alarm(said) == expected


class TestTheDay:
    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("oggi alle 23", alarm(23, 0, AlarmDay(in_days=0))),
            ("domani alle sette", alarm(7, 0, AlarmDay(in_days=1))),
            ("alle sette domani", alarm(7, 0, AlarmDay(in_days=1))),
            ("dopodomani alle 6", alarm(6, 0, AlarmDay(in_days=2))),
            ("dopo domani alle 6", alarm(6, 0, AlarmDay(in_days=2))),
            ("tra tre giorni alle 7", alarm(7, 0, AlarmDay(in_days=3))),
            ("fra un giorno alle 7", alarm(7, 0, AlarmDay(in_days=1))),
            ("tra una settimana alle 7", alarm(7, 0, AlarmDay(in_days=7))),
            ("fra due settimane alle 7", alarm(7, 0, AlarmDay(in_days=14))),
            ("lunedì alle 8", alarm(8, 0, AlarmDay(weekday=Weekday.MONDAY))),
            ("lunedi alle 8", alarm(8, 0, AlarmDay(weekday=Weekday.MONDAY))),
            ("martedì prossimo alle 8", alarm(8, 0, AlarmDay(weekday=Weekday.TUESDAY))),
            ("sabato alle 9 e mezza", alarm(9, 30, AlarmDay(weekday=Weekday.SATURDAY))),
            ("domenica alle 9", alarm(9, 0, AlarmDay(weekday=Weekday.SUNDAY))),
            ("il 15 ottobre alle 7", alarm(7, 0, AlarmDay(day=15, month=10))),
            ("il quindici ottobre alle 7", alarm(7, 0, AlarmDay(day=15, month=10))),
            ("15 ottobre alle 7", alarm(7, 0, AlarmDay(day=15, month=10))),
            ("alle 7 il 15 ottobre", alarm(7, 0, AlarmDay(day=15, month=10))),
            ("il primo maggio alle 8", alarm(8, 0, AlarmDay(day=1, month=5))),
            ("il 31 dicembre a mezzanotte", alarm(0, 0, AlarmDay(day=31, month=12))),
            ("il 15 alle 7", alarm(7, 0, AlarmDay(day=15))),
            ("il 3 alle 7 e mezza", alarm(7, 30, AlarmDay(day=3))),
        ],
    )
    def test_it_is_said_in_italian(self, said: str, expected: AlarmWords) -> None:
        assert parse_alarm(said) == expected

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("today at 11 pm", alarm(23, 0, AlarmDay(in_days=0))),
            ("tomorrow at seven", alarm(7, 0, AlarmDay(in_days=1))),
            ("at seven tomorrow", alarm(7, 0, AlarmDay(in_days=1))),
            ("the day after tomorrow at 6", alarm(6, 0, AlarmDay(in_days=2))),
            ("in three days at 7", alarm(7, 0, AlarmDay(in_days=3))),
            ("in two weeks at 7", alarm(7, 0, AlarmDay(in_days=14))),
            ("monday at 8", alarm(8, 0, AlarmDay(weekday=Weekday.MONDAY))),
            ("next friday at 8", alarm(8, 0, AlarmDay(weekday=Weekday.FRIDAY))),
            ("on the 15th of october at 7", alarm(7, 0, AlarmDay(day=15, month=10))),
            ("october 15th at 7 am", alarm(7, 0, AlarmDay(day=15, month=10))),
            ("october 15 at 7", alarm(7, 0, AlarmDay(day=15, month=10))),
            ("15 october at 7", alarm(7, 0, AlarmDay(day=15, month=10))),
            ("the twenty first of march at 9", alarm(9, 0, AlarmDay(day=21, month=3))),
            ("the first of may at 8", alarm(8, 0, AlarmDay(day=1, month=5))),
            ("on the 1st at 7", alarm(7, 0, AlarmDay(day=1))),
            ("on the fifteenth at 7", alarm(7, 0, AlarmDay(day=15))),
        ],
    )
    def test_it_is_said_in_english(self, said: str, expected: AlarmWords) -> None:
        assert parse_alarm(said) == expected

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("stasera alle 8", alarm(20, 0, AlarmDay(in_days=0))),
            ("stamattina alle 7", alarm(7, 0, AlarmDay(in_days=0))),
            ("stanotte alle 3", alarm(3, 0, AlarmDay(in_days=0))),
            ("domattina alle 7", alarm(7, 0, AlarmDay(in_days=1))),
            ("domani sera alle 8", alarm(20, 0, AlarmDay(in_days=1))),
            ("domani mattina alle 7", alarm(7, 0, AlarmDay(in_days=1))),
            ("tonight at 9", alarm(21, 0, AlarmDay(in_days=0))),
            ("tomorrow morning at 7", alarm(7, 0, AlarmDay(in_days=1))),
            ("tomorrow evening at 8", alarm(20, 0, AlarmDay(in_days=1))),
        ],
    )
    def test_it_can_carry_the_period_of_the_day(self, said: str, expected: AlarmWords) -> None:
        assert parse_alarm(said) == expected

    def test_a_day_that_is_not_said_is_none_and_the_device_takes_the_next_time(self) -> None:
        parsed = parse_alarm("alle sette")

        assert parsed is not None
        assert parsed.day == AlarmDay()

    def test_the_day_and_the_hour_are_one_phrase_in_either_order(self) -> None:
        assert parse_alarm("domani alle 7") == parse_alarm("alle 7 domani")
        assert parse_alarm("domani 7:30") == parse_alarm("7:30 domani")


class TestWhatIsNotAnAlarm:
    @pytest.mark.parametrize(
        "said",
        [
            "",
            "alle",
            "domani",
            "lunedì",
            "il 15 ottobre",
            "alle 25",
            "alle 30",
            "alle sette e 75",
            "alle 24:30",
            "alle 7 alle 8",
            "dalle sette alle otto",
            "dalle 7 alle 8",
            "alle sette di sera di mattina",
            "alle 15 di mattina",
            "domani lunedì alle 7",
            "domani dopodomani alle 7",
            "tra 10 minuti",
            "tra due ore",
            "in ten minutes",
            "in an hour",
            "every day at seven",
            "alle sette ogni giorno",
            "the first thing in the morning",
            "alle sette e",
            "alle sette meno",
            "alle sette meno ventisette quarti",
            "il 32 ottobre alle 7",
            "il 0 ottobre alle 7",
            "alle sette e 60",
            "alle sette e quattro quarti",
            "tra quattrocento giorni alle 7",
            "∞",
        ],
    )
    def test_it_is_not_guessed(self, said: str) -> None:
        assert parse_alarm(said) is None

    def test_a_number_of_days_is_not_taken_for_an_hour(self) -> None:
        assert parse_alarm("tra 3 giorni") is None  # there is a day and no hour

    @given(st.text(max_size=40))
    def test_whatever_is_said_is_read_or_it_is_not_and_never_an_error(self, said: str) -> None:
        result = parse_alarm(said)

        assert result is None or (0 <= result.hour <= 23 and 0 <= result.minute <= 59)


class TestHoursRoundTrip:
    @given(st.integers(0, 23), st.integers(0, 59), st.sampled_from(["en", "it"]))
    def test_what_is_said_of_an_hour_is_read_as_the_same_hour(
        self, hour: int, minute: int, language: str
    ) -> None:
        said = say_clock(language, hour, minute)

        assert parse_alarm(said) == alarm(hour, minute), said
