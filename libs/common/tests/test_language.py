"""Telling Italian from English in what is said, with no model (ADR 0028)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.common.language import LANGUAGES, detect_language

ITALIAN = [
    "che ore sono",
    "alza il volume",
    "che tempo fa a Torino",
    "accendi la luce del salotto",
    "spegni la lampada",
    "apri il garage",
    "riavvia il router",
    "ciao",
    "grazie",
    "come ti chiami",
    "chi ha scritto la Divina Commedia",
    "annuncia in cucina che la cena è pronta",
    "qual è la capitale della Francia",
    "come si resetta il router",
    "quanto costa la polizza",
    "dimmi una barzelletta",
    "metti la musica",
    "accendi il forno",
    "com'è l'aria a Milano",
    "e in inglese",
    "perché",
    "il secondo",
    "sì",
    "no grazie",
    "cosa c'è scritto nel manuale della lavatrice",
    "che giorno è oggi",
    "prepara la scena per il film",
    "abbassa il volume a venti",
    "volume al massimo",
]
ENGLISH = [
    "what time is it",
    "turn the volume up",
    "what is the weather like in Rome",
    "turn on the living room lamp",
    "switch off the lamp",
    "open the garage door",
    "restart the router",
    "hello",
    "thanks",
    "what is your name",
    "who wrote the Divine Comedy",
    "tell the kitchen that dinner is ready",
    "what is the capital of France",
    "how do I reset the router",
    "how much does the policy cost",
    "tell me a joke",
    "play some music",
    "turn on the oven",
    "how is the air in Milan",
    "and in Italian",
    "why",
    "the second one",
    "yes",
    "no thanks",
    "what does the washing machine manual say",
    "what day is it today",
    "set the movie scene",
    "lower the volume to twenty",
    "what's the time",
    "I don't know",
]


class TestWhatIsSaid:
    @pytest.mark.parametrize("text", ITALIAN)
    def test_italian_is_italian(self, text: str) -> None:
        assert detect_language(text) == "it"

    @pytest.mark.parametrize("text", ENGLISH)
    def test_english_is_english(self, text: str) -> None:
        assert detect_language(text) == "en"

    def test_the_case_does_not_matter(self) -> None:
        assert detect_language("CHE ORE SONO") == "it"
        assert detect_language("WHAT TIME IS IT") == "en"

    def test_an_elision_or_an_accent_is_italian(self) -> None:
        assert detect_language("l'aria") == "it"
        assert detect_language("c'è") == "it"
        assert detect_language("caffè") == "it"

    @pytest.mark.parametrize("text", ["isn't", "wouldn't", "ain't", "they've", "you'll", "I'd"])
    def test_a_contraction_is_english(self, text: str) -> None:
        assert detect_language(text) == "en"

    @pytest.mark.parametrize("text", ["dell'anno", "all'ora", "un'ora", "quest'anno"])
    def test_an_elision_is_not_a_contraction(self, text: str) -> None:
        assert detect_language(text) == "it"

    @pytest.mark.parametrize("text", ["", "   ", "12345", "?!", "Carrara", "meteo Carrara", "ok"])
    def test_what_has_no_words_to_tell_is_nobodys(self, text: str) -> None:
        assert detect_language(text) is None

    def test_as_many_words_of_each_is_nobodys(self) -> None:
        assert detect_language("ciao hello") is None

    def test_another_language_is_nobodys(self) -> None:
        assert detect_language("wie spät ist es jetzt") is None

    def test_the_more_of_a_language_wins(self) -> None:
        assert detect_language("play la musica per favore la stanza") == "it"


class TestTheLanguages:
    def test_italian_and_english_are_told_apart(self) -> None:
        assert {"it", "en"} <= LANGUAGES

    @given(st.text(max_size=200))
    def test_whatever_is_said_the_answer_is_a_language_or_nothing(self, text: str) -> None:
        assert detect_language(text) in {None, *LANGUAGES}

    @given(st.text(max_size=200))
    def test_the_same_text_always_gives_the_same_answer(self, text: str) -> None:
        assert detect_language(text) == detect_language(text)

    @given(st.sampled_from(ITALIAN + ENGLISH))
    def test_a_phrase_keeps_its_language_whatever_the_case(self, text: str) -> None:
        assert detect_language(text.upper()) == detect_language(text)
