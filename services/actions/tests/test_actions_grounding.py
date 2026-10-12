"""Whether a message says a value (ADR 0028): numbers in digits and in words in the two languages
the assistant speaks, the ends and the middle of a range, and the words of a text."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.grounding import _english, _italian, numbers_in, says, words_of
from factotum.contracts.skills import ParameterSpec, ParamType

PERCENT = ParameterSpec(type=ParamType.INTEGER, minimum=0, maximum=100)
UNBOUNDED = ParameterSpec(type=ParamType.INTEGER)
ONLY_A_MINIMUM = ParameterSpec(type=ParamType.INTEGER, minimum=0)
SIGNED = ParameterSpec(type=ParamType.INTEGER, minimum=-100, maximum=100)
FRACTION = ParameterSpec(type=ParamType.NUMBER, minimum=0, maximum=1)
TEXT = ParameterSpec(type=ParamType.STRING)
CHOICE = ParameterSpec(type=ParamType.STRING, enum=("on", "off"))
SWITCH = ParameterSpec(type=ParamType.BOOLEAN)


class TestTheWordsOfAMessage:
    def test_they_are_in_lower_case_with_no_accents_and_a_hyphen_or_an_apostrophe_breaks_them(
        self,
    ) -> None:
        assert words_of("Twenty-Five, l'Aria è già QUI!") == [
            "twenty",
            "five",
            "l",
            "aria",
            "e",
            "gia",
            "qui",
        ]

    def test_an_accent_in_the_middle_of_a_word_does_not_break_it(self) -> None:
        assert words_of("Élan été") == ["elan", "ete"]
        assert says("un été chaud", TEXT, "été")
        assert says("un ete chaud", TEXT, "été")

    def test_a_message_of_no_words_has_none(self) -> None:
        assert words_of("") == []
        assert words_of("?! ... --") == []


class TestNumbersInDigits:
    @pytest.mark.parametrize(
        ("message", "found"),
        [
            ("set the volume to 30", {30.0}),
            ("al 50 per cento", {50.0}),
            ("at 30%", {30.0}),
            ("a 0,5", {0.5}),
            ("1.5 times", {1.5}),
            ("tra 7 e 12", {7.0, 12.0}),
            ("volume 007", {7.0}),
            ("turn it up", set()),
            ("alza il volume", set()),
            ("", set()),
        ],
    )
    def test_they_are_found(self, message: str, found: set[float]) -> None:
        assert numbers_in(message) == found

    @given(st.integers(0, 10_000))
    def test_a_number_written_in_digits_is_found_wherever_it_is(self, number: int) -> None:
        assert numbers_in(f"volume {number} please") == {float(number)}
        assert numbers_in(f"{number}%") == {float(number)}


class TestNumbersInWords:
    @pytest.mark.parametrize(
        ("message", "number"),
        [
            ("zero", 0),
            ("seven", 7),
            ("eleven", 11),
            ("thirteen", 13),
            ("nineteen", 19),
            ("twenty", 20),
            ("twenty one", 21),
            ("twenty-five", 25),
            ("thirty", 30),
            ("forty-two", 42),
            ("fifty", 50),
            ("ninety nine", 99),
            ("a hundred", 100),
            ("one hundred", 100),
            ("one hundred and five", 105),
            ("one hundred five", 105),
            ("two hundred fifty", 250),
            ("nine hundred ninety-nine", 999),
        ],
    )
    def test_english(self, message: str, number: int) -> None:
        assert numbers_in(f"turn it to {message} please") == {float(number)}

    @pytest.mark.parametrize(
        ("message", "number"),
        [
            ("zero", 0),
            ("sette", 7),
            ("dodici", 12),
            ("sedici", 16),
            ("diciassette", 17),
            ("diciannove", 19),
            ("venti", 20),
            ("ventuno", 21),
            ("ventidue", 22),
            ("ventitré", 23),
            ("ventitre", 23),
            ("ventiquattro", 24),
            ("venticinque", 25),
            ("ventisei", 26),
            ("ventisette", 27),
            ("ventotto", 28),
            ("ventinove", 29),
            ("trenta", 30),
            ("trentuno", 31),
            ("trentotto", 38),
            ("quarantuno", 41),
            ("cinquanta", 50),
            ("ottantuno", 81),
            ("ottantotto", 88),
            ("novantotto", 98),
            ("novantanove", 99),
            ("cento", 100),
            ("centouno", 101),
            ("centuno", 101),
            ("centotto", 108),
            ("centoottanta", 180),
            ("centottanta", 180),
            ("duecentocinquanta", 250),
            ("trecentotto", 308),
            ("trecentoventuno", 321),
            ("novecentonovantanove", 999),
        ],
    )
    def test_italian(self, message: str, number: int) -> None:
        assert numbers_in(f"metti il volume a {message} per favore") == {float(number)}

    def test_the_longest_number_is_the_one_that_is_read(self) -> None:
        assert numbers_in("one hundred and five") == {105.0}
        assert numbers_in("twenty five") == {25.0}
        assert numbers_in("twenty and five") == {20.0, 5.0}

    def test_every_number_up_to_999_is_read_back_as_itself_in_every_way_it_is_written(self) -> None:
        for number in range(1000):
            for form in _english(number):
                assert numbers_in(" ".join(form)) == {float(number)}, form
            for word in _italian(number):
                assert numbers_in(word) == {float(number)}, word

    def test_several_numbers_in_one_message_are_all_found(self) -> None:
        assert numbers_in("alza a venti e poi abbassa a 30, anzi a trentacinque") == {
            20.0,
            30.0,
            35.0,
        }

    def test_a_percent_is_not_a_hundred(self) -> None:
        assert numbers_in("al 50 per cento") == {50.0}
        assert numbers_in("per cento") == set()
        assert numbers_in("per cento e cento") == {100.0}
        assert not says("alza la luce al 50 per cento", PERCENT, 100)

    def test_the_words_of_what_is_not_a_number_are_not_numbers(self) -> None:
        for message in ("un po'", "a bit", "alza il volume", "turn it up", "più forte"):
            assert numbers_in(message) == set(), message

    def test_the_net_is_wide_on_purpose(self) -> None:
        """A word that is a number and something else is a number: the check only lets through a
        value that a model wrote, so a number too many costs nothing."""
        assert numbers_in("sei qui") == {6.0}


class TestWhetherANumberIsSaid:
    def test_a_number_that_the_message_gives_is_said(self) -> None:
        assert says("set the volume to 30", PERCENT, 30)
        assert says("alza il volume a 70", PERCENT, 70)

    def test_a_whole_number_is_the_same_as_the_float_that_equals_it(self) -> None:
        assert says("to 30", PERCENT, 30.0)
        assert says("0,5", FRACTION, 0.5)

    def test_a_number_that_nobody_said_is_not(self) -> None:
        assert not says("turn it up", PERCENT, 100)
        assert not says("alza il volume", PERCENT, 10)
        assert not says("alza a 70", PERCENT, 80)
        assert not says("", PERCENT, 0)

    @pytest.mark.parametrize(
        ("message", "number"),
        [
            ("metti il volume a cinquanta", 50),
            ("volume at fifty percent", 50),
            ("could you dim the lights to thirty percent", 30),
            ("abbassa il volume di quindici", 15),
            ("alza di venticinque", 25),
        ],
    )
    def test_a_number_in_words_is_said(self, message: str, number: int) -> None:
        assert says(message, PERCENT, number)

    def test_the_sign_is_not_looked_for(self) -> None:
        assert says("minus ten", SIGNED, -10)
        assert says("-10", SIGNED, -10)

    @given(st.integers(0, 100))
    def test_a_level_in_digits_is_said(self, level: int) -> None:
        assert says(f"metti il volume a {level}", PERCENT, level)

    @given(st.integers(0, 100))
    def test_with_no_number_and_no_extreme_in_the_message_no_level_is_said(
        self, level: int
    ) -> None:
        assert not says("alza il volume", PERCENT, level)
        assert not says("turn it up a bit", PERCENT, level)


class TestTheEndsAndTheMiddleOfARange:
    @pytest.mark.parametrize(
        ("message", "number"),
        [
            ("metti il volume al massimo", 100),
            ("to the max", 100),
            ("at full volume", 100),
            ("maximum", 100),
            ("la luce piena", 100),
            ("al minimo", 0),
            ("at the lowest", 0),
            ("minimum please", 0),
            ("a metà", 50),
            ("a meta", 50),
            ("half", 50),
            ("halfway", 50),
            ("a mezzo volume", 50),
        ],
    )
    def test_they_are_the_bounds_of_the_parameter_and_not_a_number_that_a_model_chose(
        self, message: str, number: int
    ) -> None:
        assert says(message, PERCENT, number)

    @pytest.mark.parametrize(
        ("message", "number"),
        [
            ("al massimo", 90),
            ("al massimo", 0),
            ("al massimo", 50),
            ("al minimo", 100),
            ("al minimo", 1),
            ("a metà", 100),
            ("a metà", 0),
            ("half", 49),
        ],
    )
    def test_and_only_those(self, message: str, number: int) -> None:
        assert not says(message, PERCENT, number)

    def test_a_parameter_with_no_such_bound_has_no_such_extreme(self) -> None:
        assert not says("al massimo", UNBOUNDED, 100)
        assert not says("al minimo", UNBOUNDED, 0)
        assert not says("a metà", ONLY_A_MINIMUM, 50)
        assert not says("al massimo", ONLY_A_MINIMUM, 100)
        assert says("al minimo", ONLY_A_MINIMUM, 0)

    def test_the_middle_is_the_middle_of_the_range_that_is_declared(self) -> None:
        assert says("half", FRACTION, 0.5)
        assert says("a metà", SIGNED, 0)
        assert not says("a metà", SIGNED, 50)


class TestWhetherATextIsSaid:
    def test_a_text_is_made_of_words_that_the_message_has(self) -> None:
        assert says("che tempo fa a Torino", TEXT, "Torino")
        assert says("tell everyone that dinner is ready", TEXT, "dinner is ready")
        assert says("annuncia che la cena è pronta", TEXT, "la cena è pronta")

    def test_case_and_accents_do_not_matter(self) -> None:
        assert says("meteo a Forlì", TEXT, "forli")
        assert says("meteo a forli", TEXT, "Forlì")
        assert says("what about NAPLES", TEXT, "Naples")

    def test_an_apostrophe_is_a_word_break(self) -> None:
        assert says("com'è l'aria a Milano", TEXT, "aria")
        assert says("com'è l'aria a Milano", TEXT, "l'aria")

    def test_the_order_of_the_words_does_not_matter(self) -> None:
        assert says("the dinner is ready", TEXT, "ready dinner")

    def test_a_text_that_is_not_in_the_message_is_not_said(self) -> None:
        assert not says("che tempo fa", TEXT, "Roma")
        assert not says("che tempo fa a Torino", TEXT, "Torino e Milano")
        assert not says("", TEXT, "Roma")

    def test_a_part_of_a_word_is_not_the_word(self) -> None:
        assert not says("meteo a Roma", TEXT, "Rom")
        assert not says("weather in Rome", TEXT, "ro")

    def test_a_text_with_no_words_is_not_said(self) -> None:
        assert not says("anything at all", TEXT, "...")

    def test_a_number_that_a_text_is_made_of_is_looked_for_as_the_word_it_is(self) -> None:
        assert says("volume 5", TEXT, "5")
        assert not says("volume 6", TEXT, "5")

    def test_where_words_are_written_with_no_spaces_the_text_is_looked_for_inside(self) -> None:
        assert says("東京の天気は", TEXT, "東京")
        assert not says("東京の天気は", TEXT, "大阪")
        assert says("วันนี้อากาศที่กรุงเทพ", TEXT, "กรุงเทพ")


class TestWhatIsNotLookedFor:
    @pytest.mark.parametrize("message", ["accendi la luce", "", "nothing of the sort"])
    def test_a_choice_from_a_list_is_the_models_reading_and_is_not_refused(
        self, message: str
    ) -> None:
        assert says(message, CHOICE, "on")
        assert says(message, CHOICE, "off")

    @pytest.mark.parametrize("message", ["accendi la luce", ""])
    def test_a_yes_or_no_is_not_refused(self, message: str) -> None:
        assert says(message, SWITCH, True)
        assert says(message, SWITCH, False)

    def test_a_yes_or_no_is_not_taken_for_the_number_that_it_is_in_python(self) -> None:
        assert says("no number here", SWITCH, True)
        assert not says("no number here", PERCENT, 1)
