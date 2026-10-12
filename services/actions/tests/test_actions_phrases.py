import string

import pytest

from factotum.actions.phrases import (
    _CATALOG,
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Say,
    language_of,
    refusal,
    say,
)
from factotum.contracts.authorization import Reason


def fields_of(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class TestTheCatalog:
    @pytest.mark.parametrize("language", sorted(LANGUAGES))
    def test_every_language_has_every_sentence(self, language: str) -> None:
        assert set(_CATALOG[language]) == set(Say)

    @pytest.mark.parametrize("key", list(Say))
    def test_a_sentence_has_the_same_blanks_in_every_language(self, key: Say) -> None:
        blanks = {language: fields_of(_CATALOG[language][key]) for language in LANGUAGES}

        assert len({frozenset(b) for b in blanks.values()}) == 1, blanks

    def test_english_and_italian_are_here(self) -> None:
        assert {"en", "it"} <= LANGUAGES
        assert DEFAULT_LANGUAGE in LANGUAGES


class TestSaying:
    def test_a_sentence_is_said_in_the_language_of_the_person(self) -> None:
        assert say("en", Say.CANCELLED) == "Okay, I will not do it."
        assert say("it", Say.CANCELLED) == "Va bene, non lo faccio."

    @pytest.mark.parametrize("language", [None, "fr", "", "xx", "EN"])
    def test_a_language_there_are_no_words_for_is_answered_in_english(
        self, language: str | None
    ) -> None:
        assert say(language, Say.DONE) == "Done."
        assert language_of(language) == "en"

    def test_the_blanks_are_filled(self) -> None:
        assert say("en", Say.WHICH_DEVICE, names="the lamp or the door") == (
            "Which one do you mean: the lamp or the door?"
        )
        assert say("it", Say.NEEDS_VALUES, names="level") == "Mi serve ancora: level."
        assert say("en", Say.CONFIRM, skill="garage-door") == "Do you want me to run garage-door?"

    def test_a_blank_that_a_caller_did_not_fill_is_an_error_and_not_a_silence(self) -> None:
        with pytest.raises(KeyError):
            say("en", Say.CONFIRM)

    @pytest.mark.parametrize("key", [Say.FAILED, Say.RUNNER_UNAVAILABLE, Say.COMMAND_FAILED])
    def test_a_sentence_about_a_skill_that_failed_names_it_in_every_language(
        self, key: Say
    ) -> None:
        for language in sorted(LANGUAGES):
            assert fields_of(_CATALOG[language][key]) == {"skill"}
            assert "garage-door" in say(language, key, skill="garage-door")

    def test_the_sentences_of_a_skill_that_failed(self) -> None:
        assert say("en", Say.FAILED, skill="lamp") == "The lamp skill failed."
        assert say("it", Say.FAILED, skill="lamp") == "La skill lamp ha fallito."
        assert say("it", Say.RUNNER_UNAVAILABLE, skill="lamp") == (
            "Non riesco a eseguire la skill lamp in questo momento."
        )
        assert say("en", Say.COMMAND_FAILED, skill="lamp") == (
            "I asked the device to run the lamp skill, and it did not do it."
        )


class TestWhyNot:
    def test_a_device_that_may_not_start_something_is_told_so(self) -> None:
        assert refusal("en", (Reason.EXCEEDS_DEVICE_MAX_EFFECT,)) == (
            "This device is not allowed to do that."
        )
        assert refusal("it", (Reason.SKILL_NOT_ALLOWED_ON_DEVICE,)) == (
            "Questo dispositivo non può farlo."
        )

    @pytest.mark.parametrize(
        "reason", [Reason.CROSS_TENANT, Reason.SKILL_NOT_VISIBLE, Reason.TARGET_NOT_VISIBLE]
    )
    def test_what_a_person_may_not_see_is_not_told_to_exist(self, reason: Reason) -> None:
        assert refusal("en", (reason,)) == "I cannot do that."
        assert refusal("en", (Reason.EXCEEDS_DEVICE_MAX_EFFECT, reason)) == "I cannot do that."

    def test_no_reason_at_all_is_a_plain_no(self) -> None:
        assert refusal("en", ()) == "I cannot do that."
