"""What the stand-in of the chat model and of the small model that plans does with Italian, and with
what a hurried ear makes of it: the house answers in the language it was asked in, and understands a
command that was heard with the wrong accent or a letter wrong, as a competent model does."""

import pytest

from factotum.e2e.models import (
    CLOSING,
    CLOSING_IT,
    FROM_YOUR_DOCUMENTS,
    FROM_YOUR_DOCUMENTS_IT,
    NOT_FOUND,
    NOT_FOUND_IT,
    OFFER,
    OFFER_IT,
    answer_from,
    is_italian,
    plan_for,
)

SOURCE = (
    "=== SOURCES abc123 ===\n[1] Manual\nPer pulire il filtro apri lo sportello e svita il tappo."
    "\n=== END SOURCES abc123 ==="
)


class TestWhatIsItalian:
    @pytest.mark.parametrize(
        "text",
        [
            "Come si pulisce il filtro della lavatrice?",
            "Dov'è il router?",
            "Accendi la luce del soggiorno.",
            "Sì, apri pure.",
            "Forse più tardi.",
        ],
    )
    def test_it_is_told_by_a_word_that_is_not_english_or_by_an_accent(self, text: str) -> None:
        assert is_italian(text)

    @pytest.mark.parametrize(
        "text",
        [
            "How do I reset the router?",
            "Set the volume to 20.",
            "Open the garage door.",
            "Turn on my lamp.",
            "Yes.",
            "What is the reset button for?",
        ],
    )
    def test_the_questions_and_commands_of_the_english_tests_are_not(self, text: str) -> None:
        assert not is_italian(text)


class TestAnsweringInTheLanguageOfTheQuestion:
    def test_an_english_question_is_answered_in_english(self) -> None:
        answer = answer_from(f"{SOURCE}\n\nMessage: How do I clean the filter?")

        assert answer.startswith(FROM_YOUR_DOCUMENTS)
        assert answer.endswith(CLOSING)

    def test_an_italian_question_is_answered_in_italian(self) -> None:
        answer = answer_from(f"{SOURCE}\n\nMessage: Come si pulisce il filtro?")

        assert answer.startswith(FROM_YOUR_DOCUMENTS_IT)
        assert answer.endswith(CLOSING_IT)
        assert "[1]" in answer

    def test_what_nothing_answers_is_said_in_the_language_it_was_asked_in(self) -> None:
        assert answer_from("Message: How do I clean the filter?") == f"{NOT_FOUND} {OFFER}"
        assert answer_from("Message: Come si pulisce il filtro?") == f"{NOT_FOUND_IT} {OFFER_IT}"


def planned(message: str) -> dict[str, object]:
    return plan_for(f"Message: {message}")


class TestPlanningWhatWasHeard:
    @pytest.mark.parametrize(
        ("heard", "action"),
        [
            ("Apri il garage.", "open"),
            ("Api il garaf!", "open"),
            ("Apil Garav", "open"),
            ("Chiudi il garage.", "close"),
            ("Chiudì il garrar.", "close"),
            ("Close the garage door.", "close"),
            ("Open the garage door.", "open"),
        ],
    )
    def test_a_garage_that_was_heard_a_little_wrong_is_still_the_garage(
        self, heard: str, action: str
    ) -> None:
        plan = planned(heard)

        assert plan["skill"] == "garage-door"
        assert plan["parameters"] == {"action": action}

    def test_a_lamp_that_was_heard_with_the_wrong_accents_is_still_the_lamp(self) -> None:
        plan = planned("Spegnì la làmpada della camera.")

        assert (plan["skill"], plan["device"], plan["parameters"]) == (
            "lamp",
            "Bedroom lamp",
            {"state": "off"},
        )

    def test_a_question_about_the_garage_is_not_a_command(self) -> None:
        assert planned("Is the garage open?")["skill"] is None

    def test_the_language_of_a_command_is_the_one_it_was_given_in(self) -> None:
        assert planned("Apri il garage.")["language"] == "it"
        assert planned("Open the garage door.")["language"] == "en"
        assert planned("Set the volume to 20.")["language"] == "en"

    def test_the_door_asks_in_the_language_of_the_command(self) -> None:
        assert planned("Apri il garage.")["question"] == "Vuoi davvero che apra il garage?"
        assert planned("Close the garage door.")["question"] == (
            "Do you really want me to close the garage door?"
        )

    def test_a_number_is_the_level_of_a_volume(self) -> None:
        assert planned("Imposta il volume a 30.")["parameters"] == {"level": 30}
        assert planned("Set the volume to 20.")["parameters"] == {"level": 20}
