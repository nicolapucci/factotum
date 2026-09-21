import asyncio
from typing import Any

import pytest

from factotum.actions.confirmations import (
    MAX_ANSWER_CHARS,
    Answer,
    ConfirmationReader,
    normalize,
    read_by_words,
)
from factotum.actions.testing import ScriptedChat
from factotum.contracts.errors import GatewayUnavailableError, ProviderTimeoutError
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.models import ModelRole
from factotum.contracts.testing import make_tenant_id

OWNER = Owner.of_subject(Subject.tenant(make_tenant_id()))
QUESTION = "Do you want me to open the garage door?"


class TestByTheWords:
    @pytest.mark.parametrize(
        "said",
        [
            "yes",
            "Yes!",
            "  YES  ",
            "yeah",
            "sure",
            "Okay.",
            "ok",
            "go ahead",
            "Go ahead!",
            "do it",
            "yes, please",
            "of course",
            "confirm",
            "sì",
            "Sì!",
            "si",
            "certo",
            "va bene",
            "Va bene.",
            "fai pure",
            "fallo",
            "conferma",
            "sì, grazie",
            "assolutamente sì",
        ],
    )
    def test_the_ways_of_saying_yes_are_a_yes(self, said: str) -> None:
        assert read_by_words(said) is Answer.YES

    @pytest.mark.parametrize(
        "said",
        [
            "no",
            "No.",
            "nope",
            "don't",
            "Don't do it!",
            "cancel",
            "stop",
            "never mind",
            "not now",
            "no thanks",
            "annulla",
            "Annulla!",
            "lascia stare",
            "lascia perdere",
            "non farlo",
            "no grazie",
            "fermati",
        ],
    )
    def test_the_ways_of_saying_no_are_a_no(self, said: str) -> None:
        assert read_by_words(said) is Answer.NO

    @pytest.mark.parametrize(
        "said",
        [
            "yes and turn on the lamp",
            "yes but only halfway",
            "no wait",
            "what time is it",
            "turn on the lamp",
            "maybe",
            "sì ma a metà",
            "non lo so",
            "",
            "   ",
            "!!!",
        ],
    )
    def test_whatever_leaves_a_doubt_is_left_to_the_model(self, said: str) -> None:
        assert read_by_words(said) is None

    def test_words_are_compared_without_case_accents_or_punctuation(self) -> None:
        assert normalize("  Sì,   CERTO!! ") == "si certo"
        assert normalize("Don't!") == "don t"


class TestByTheModel:
    async def test_what_the_words_settle_is_settled_without_asking_a_model(self) -> None:
        chat = ScriptedChat()

        assert await ConfirmationReader(chat).read(OWNER, QUESTION, "sure") is Answer.YES
        assert await ConfirmationReader(chat).read(OWNER, QUESTION, "no thanks") is Answer.NO
        assert chat.requests == []

    @pytest.mark.parametrize("answer", list(Answer))
    async def test_the_rest_is_read_by_a_small_model(self, answer: Answer) -> None:
        chat = ScriptedChat()
        chat.say({"reason": "as told", "answer": answer.value})

        read = await ConfirmationReader(chat).read(OWNER, QUESTION, "why not, go on then")

        assert read is answer
        [request] = chat.asked
        assert request.role is ModelRole.CHAT_FAST
        assert request.temperature == 0
        assert request.response_schema is not None
        assert request.messages[-1].content == (
            f"Question: {QUESTION}\nAnswer: why not, go on then"
        )

    async def test_the_model_is_shown_what_a_yes_looks_like_as_turns_of_its_own_conversation(
        self,
    ) -> None:
        chat = ScriptedChat()
        chat.say({"reason": "x", "answer": "other"})

        await ConfirmationReader(chat).read(OWNER, QUESTION, "hmm, maybe")

        [request] = chat.asked
        roles = [m.role.value for m in request.messages]
        assert roles[0] == "system"
        assert roles[1:-1] == ["user", "assistant"] * ((len(roles) - 2) // 2)
        assert roles[-1] == "user"

    async def test_it_is_asked_for_the_owner_of_the_person_who_spoke(self) -> None:
        chat = ScriptedChat()
        chat.say({"reason": "x", "answer": "yes"})

        await ConfirmationReader(chat).read(OWNER, QUESTION, "hmm, why not")

        assert chat.requests[0][0] == OWNER


class TestWhatIsNotAYesIsNotOne:
    @pytest.mark.parametrize(
        "failure",
        [
            GatewayUnavailableError("the model gateway could not be reached"),
            ProviderTimeoutError("the model did not answer"),
        ],
    )
    async def test_a_model_that_cannot_answer_is_no_yes(self, failure: Exception) -> None:
        chat = ScriptedChat()
        chat.say(failure)

        assert await ConfirmationReader(chat).read(OWNER, QUESTION, "hmm, maybe") is Answer.OTHER

    async def test_a_model_that_does_not_answer_in_time_is_no_yes(self) -> None:
        class Slow(ScriptedChat):
            async def chat(self, owner: Owner, request: Any) -> Any:
                await asyncio.sleep(5)

        reader = ConfirmationReader(Slow(), timeout_s=0.05)

        assert await reader.read(OWNER, QUESTION, "hmm, maybe") is Answer.OTHER

    @pytest.mark.parametrize(
        "nonsense",
        [
            {"reason": "x", "answer": "affirmative"},
            {"reason": "x"},
            {"answer": "yes", "reason": "x" * 500},
            {"reason": "x", "answer": "yes", "extra": 1},
            "yes",
        ],
    )
    async def test_an_answer_that_is_not_one_of_the_three_is_no_yes(self, nonsense: Any) -> None:
        chat = ScriptedChat()
        chat.say(nonsense)

        assert await ConfirmationReader(chat).read(OWNER, QUESTION, "hmm, maybe") is Answer.OTHER

    async def test_what_is_long_is_not_a_yes_and_is_not_even_read(self) -> None:
        chat = ScriptedChat()

        read = await ConfirmationReader(chat).read(OWNER, QUESTION, "yes " * MAX_ANSWER_CHARS)

        assert read is Answer.OTHER
        assert chat.requests == []
