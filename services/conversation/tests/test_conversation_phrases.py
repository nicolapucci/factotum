"""What the assistant says when it has nothing to answer from (ADR 0028)."""

import itertools

import pytest

from factotum.common.language import LANGUAGES as TOLD_APART
from factotum.conversation.phrases import (
    _CATALOG,
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Say,
    language_of,
    nothing_to_answer_from,
    say,
)


class TestTheCatalog:
    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_every_language_has_every_sentence(self, language: str) -> None:
        assert set(_CATALOG[language]) == set(Say)

    def test_every_sentence_is_a_sentence_that_ends(self) -> None:
        for language, sentences in _CATALOG.items():
            for key, text in sentences.items():
                assert text.endswith("."), (language, key)
                assert text == text.strip(), (language, key)

    def test_no_sentence_has_a_blank_to_fill(self) -> None:
        for sentences in _CATALOG.values():
            for text in sentences.values():
                assert "{" not in text
                assert "}" not in text

    def test_every_language_of_the_catalog_can_be_told_from_the_others(self) -> None:
        """What a message is in is read by ``factotum.common.language``, which counts the small
        words of the languages it knows: a language with words here and none there would be
        answered in the default for ever."""
        assert LANGUAGES <= TOLD_APART

    def test_english_and_italian_are_here_and_english_is_the_default(self) -> None:
        assert {"en", "it"} <= LANGUAGES
        assert DEFAULT_LANGUAGE == "en"

    def test_the_sentences_are_different_from_one_another_in_every_language(self) -> None:
        for sentences in _CATALOG.values():
            assert len(set(sentences.values())) == len(sentences)


class TestWhatIsSaid:
    def test_the_two_missing_things_in_english(self) -> None:
        assert (
            nothing_to_answer_from("en", skills_away=False, documents_away=False)
            == "I found no skill that fits, and no relevant document."
        )

    def test_the_two_missing_things_in_italian(self) -> None:
        assert (
            nothing_to_answer_from("it", skills_away=False, documents_away=False)
            == "Non ho trovato una skill adatta, né documenti pertinenti."
        )

    def test_the_documents_that_could_not_be_reached(self) -> None:
        assert nothing_to_answer_from("en", skills_away=False, documents_away=True) == (
            "I found no skill that fits, and I cannot reach your documents right now."
        )
        assert nothing_to_answer_from("it", skills_away=False, documents_away=True) == (
            "Non ho trovato una skill adatta, e non riesco a consultare i tuoi documenti "
            "in questo momento."
        )

    def test_the_skills_that_could_not_be_reached(self) -> None:
        assert nothing_to_answer_from("en", skills_away=True, documents_away=False) == (
            "I cannot reach the skills right now, and I found no relevant document."
        )
        assert nothing_to_answer_from("it", skills_away=True, documents_away=False) == (
            "Non riesco a consultare le skill in questo momento, e non ho trovato documenti "
            "pertinenti."
        )

    def test_neither_could_be_reached(self) -> None:
        assert nothing_to_answer_from("en", skills_away=True, documents_away=True) == (
            "I cannot reach the skills or your documents right now."
        )
        assert nothing_to_answer_from("it", skills_away=True, documents_away=True) == (
            "Non riesco a consultare né le skill né i tuoi documenti in questo momento."
        )

    def test_what_nobody_remembers(self) -> None:
        assert say("en", Say.NOTHING_REMEMBERED) == "I do not remember what was said before."
        assert say("it", Say.NOTHING_REMEMBERED) == "Non ricordo cosa è stato detto prima."

    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_each_combination_has_a_sentence_of_its_own(self, language: str) -> None:
        sentences = {
            nothing_to_answer_from(language, skills_away=skills, documents_away=documents)
            for skills, documents in itertools.product((False, True), repeat=2)
        }

        assert len(sentences) == 4


class TestTheLanguageOfTheAnswer:
    @pytest.mark.parametrize("language", [None, "fr", "", "xx", "EN"])
    def test_a_language_there_are_no_words_for_is_answered_in_the_default(
        self, language: str | None
    ) -> None:
        assert language_of(language) == "en"
        assert language_of(language, "it") == "it"

    def test_a_default_there_are_no_words_for_is_english(self) -> None:
        assert language_of(None, "fr") == "en"

    def test_a_language_with_words_is_the_one_that_is_answered_in(self) -> None:
        assert language_of("it", "en") == "it"
        assert say("it", Say.NOTHING_REMEMBERED, "en") == "Non ricordo cosa è stato detto prima."
        assert say(None, Say.NOTHING_REMEMBERED, "it") == "Non ricordo cosa è stato detto prima."
