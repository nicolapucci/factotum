"""A turn that begins by asking the actions service whether the message asks for something to be
done: what it says is the answer when it does, and when it does not, or cannot be asked, the message
is the question it always was."""

import logging
from collections.abc import Sequence
from uuid import uuid4

import pytest

from factotum.contracts.actions import ActCode, ActOutcome, ActStatus
from factotum.contracts.conversation import (
    Delta,
    Finished,
    Phase,
    Reference,
    ReferenceKind,
    Started,
    Status,
    TurnEvent,
    TurnWarning,
    WarningCode,
)
from factotum.contracts.errors import ActionsUnavailableError
from factotum.contracts.identity import Owner, Principal
from factotum.contracts.ids import DeviceId
from factotum.contracts.testing import (
    make_tenant_id,
    make_tenant_principal,
    make_user_id,
    make_user_principal,
)
from factotum.conversation.store import InMemoryStore
from factotum.conversation.testing import Harness, passage
from factotum.conversation.turn import TurnSettings

TENANT = make_tenant_id()
USER = make_user_id()
KITCHEN = DeviceId(uuid4())
LAMP = "turn on the living room lamp"
DONE = ActOutcome(status=ActStatus.DONE, reply="The living room lamp is on.", skill="lamp")


def alice(device: DeviceId | None = None) -> Principal:
    return make_user_principal(tenant_id=TENANT, user_id=USER, device_id=device)


def kitchen() -> Principal:
    return make_tenant_principal(tenant_id=TENANT, device_id=KITCHEN)


def phases(events: Sequence[TurnEvent]) -> list[Phase]:
    return [e.phase for e in events if isinstance(e, Status)]


def said(events: Sequence[TurnEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, Delta))


def warnings(events: Sequence[TurnEvent]) -> list[WarningCode]:
    return [e.code for e in events if isinstance(e, TurnWarning)]


def finished(events: Sequence[TurnEvent]) -> Finished:
    [last] = [e for e in events if isinstance(e, Finished)]
    return last


class TestAMessageThatAsksForSomethingToBeDone:
    async def test_what_the_actions_service_says_is_the_answer_and_nothing_else_is_asked(
        self,
    ) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(DONE)

        events = await harness.turn(alice(KITCHEN), LAMP)

        assert [e.type for e in events] == ["started", "status", "status", "delta", "finished"]
        assert phases(events) == [Phase.UNDERSTANDING, Phase.ACTING]
        assert said(events) == "The living room lamp is on."
        assert harness.documents.queries == []
        assert harness.chat.streams == []
        assert harness.chat.classifications == []

    async def test_the_turn_finishes_with_nothing_of_anybody_elses_in_it(self) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(DONE)

        events = await harness.turn(alice(), LAMP)

        assert finished(events) == Finished(
            reference=ReferenceKind.NONE,
            exchanges_used=0,
            passages_used=0,
            untrusted_content=False,
        )

    async def test_the_actions_service_is_asked_for_the_person_and_the_device_and_the_message(
        self,
    ) -> None:
        harness = Harness(actions=True)
        personal = alice(KITCHEN)

        await harness.turn(personal, LAMP)

        assert harness.actions.asked == [(personal, LAMP)]

    async def test_for_a_shared_device_it_is_the_tenant_that_the_service_is_asked_for(self) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(DONE)

        events = await harness.turn(kitchen(), LAMP)

        assert said(events) == "The living room lamp is on."
        assert harness.actions.asked == [(kitchen(), LAMP)]

    @pytest.mark.parametrize(
        "outcome",
        [
            ActOutcome(
                status=ActStatus.NEEDS_CONFIRMATION,
                reply="Do you really want me to open the garage door?",
                skill="garage-door",
            ),
            ActOutcome(
                status=ActStatus.NEEDS_MORE,
                reply="Which lamp?",
                code=ActCode.AMBIGUOUS_DEVICE,
                skill="lamp",
            ),
            ActOutcome(
                status=ActStatus.CANCELLED, reply="All right, I will not.", code=ActCode.DECLINED
            ),
            ActOutcome(
                status=ActStatus.REFUSED, reply="I cannot do that from here.", code=ActCode.DENIED
            ),
            ActOutcome(
                status=ActStatus.FAILED, reply="The lamp skill failed.", code=ActCode.SKILL_FAILED
            ),
        ],
        ids=["asks", "needs-more", "cancelled", "refused", "failed"],
    )
    async def test_whatever_became_of_it_what_is_said_is_the_answer(
        self, outcome: ActOutcome
    ) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(outcome)

        events = await harness.turn(alice(KITCHEN), "do the thing")

        assert said(events) == outcome.reply
        assert isinstance(events[-1], Finished)
        assert harness.documents.queries == []

    async def test_it_is_remembered_like_any_answer(self) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(DONE)

        events = await harness.turn(alice(), LAMP)

        [started] = [e for e in events if isinstance(e, Started)]
        found, exchanges = await harness.store.get_conversation(
            TENANT, Owner.of_subject(alice().acts_as), started.conversation_id
        )
        [exchange] = exchanges
        assert (exchange.question, exchange.answer) == (LAMP, "The living room lamp is on.")
        assert (exchange.id, exchange.untrusted, exchange.citations) == (
            started.exchange_id,
            False,
            (),
        )
        assert found.exchanges == 1

    async def test_and_what_was_said_is_there_for_a_message_that_carries_it_on(self) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(DONE)
        await harness.turn(alice(), LAMP)
        harness.chat.decisions.append(Reference(kind=ReferenceKind.CONTINUATION))

        events = await harness.turn(alice(), "and the kitchen one?")

        given = "\n".join(m.content for m in harness.chat.streams[-1].messages)
        assert f"You: {LAMP}\nAssistant: The living room lamp is on." in given
        assert finished(events).exchanges_used == 1

    async def test_a_failing_save_does_not_take_the_answer_away(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Broken(InMemoryStore):
            async def add_exchange(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
                raise RuntimeError("disk full")

        harness = Harness(actions=True)
        harness.store = Broken()
        harness.engine._store = harness.store  # type: ignore[assignment]
        harness.actions.outcomes.append(DONE)

        with caplog.at_level(logging.ERROR, logger="factotum.conversation"):
            events = await harness.turn(alice(), LAMP)

        assert said(events) == "The living room lamp is on."
        assert isinstance(events[-1], Finished)
        assert "could not be saved" in caplog.text

    async def test_what_was_done_is_logged_without_what_was_said(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(DONE)

        with caplog.at_level(logging.INFO, logger="factotum.conversation"):
            await harness.turn(alice(), LAMP)

        [record] = [r for r in caplog.records if r.getMessage() == "a message was acted on"]
        assert (record.status, record.skill) == ("done", "lamp")  # type: ignore[attr-defined]
        assert LAMP not in caplog.text


class TestAMessageThatDoesNot:
    async def test_it_is_a_question_for_the_documents_as_before(self) -> None:
        harness = Harness(actions=True)
        harness.documents.passages = [passage("Hold the button.")]
        harness.chat.answers.append(["Hold it ", "for ten seconds [1]."])

        events = await harness.turn(alice(), "how do I reset the router?")

        assert phases(events) == [Phase.UNDERSTANDING, Phase.SEARCHING, Phase.ANSWERING]
        assert said(events) == "Hold it for ten seconds [1]."
        assert harness.actions.asked == [(alice(), "how do I reset the router?")]
        assert harness.documents.queries != []

    async def test_being_read_is_said_once_when_the_past_is_read_too(self) -> None:
        harness = Harness(actions=True)
        await harness.turn(alice(), "how do I reset the router?")
        harness.chat.decisions.append(Reference(kind=ReferenceKind.CONTINUATION))

        events = await harness.turn(alice(), "and in Italian?")

        assert phases(events).count(Phase.UNDERSTANDING) == 1
        assert phases(events)[:2] == [Phase.UNDERSTANDING, Phase.RECALLING]
        assert len(harness.chat.classifications) == 1

    async def test_every_message_is_asked_about_first_the_ones_that_answer_a_question_too(
        self,
    ) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(
            ActOutcome(
                status=ActStatus.NEEDS_CONFIRMATION,
                reply="Do you really want me to open the garage door?",
            )
        )

        await harness.turn(alice(KITCHEN), "open the garage door")
        await harness.turn(alice(KITCHEN), "yes")

        assert [message for _, message in harness.actions.asked] == ["open the garage door", "yes"]

    async def test_without_an_actions_service_nothing_is_asked_and_nothing_is_said_about_it(
        self,
    ) -> None:
        harness = Harness()

        events = await harness.turn(alice(), "how do I reset the router?")

        assert phases(events) == [Phase.SEARCHING, Phase.ANSWERING]
        assert warnings(events) == []
        assert harness.actions.asked == []


class TestWhenTheActionsServiceCannotBeAsked:
    async def test_the_message_is_answered_as_a_question_and_the_turn_says_so(self) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(
            ActionsUnavailableError("the actions service could not be reached")
        )
        harness.documents.passages = [passage("Hold the button.")]
        harness.chat.answers.append(["Hold it [1]."])

        events = await harness.turn(alice(), LAMP)

        assert warnings(events) == [WarningCode.ACTIONS_UNAVAILABLE]
        assert said(events) == "Hold it [1]."
        assert phases(events) == [Phase.UNDERSTANDING, Phase.SEARCHING, Phase.ANSWERING]
        assert isinstance(events[-1], Finished)

    async def test_a_message_that_the_service_could_not_read_is_the_same(self) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(ActOutcome.not_read())
        harness.documents.passages = [passage("Hold the button.")]
        harness.chat.answers.append(["Hold it [1]."])

        events = await harness.turn(alice(), LAMP)

        assert warnings(events) == [WarningCode.ACTIONS_UNAVAILABLE]
        assert said(events) == "Hold it [1]."
        assert phases(events) == [Phase.UNDERSTANDING, Phase.SEARCHING, Phase.ANSWERING]

    async def test_a_message_that_was_read_and_is_a_question_has_no_warning(self) -> None:
        harness = Harness(actions=True)

        events = await harness.turn(alice(), "how do I reset the router?")

        assert warnings(events) == []

    async def test_a_warning_comes_before_what_the_answer_goes_on_with(self) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(ActionsUnavailableError("gone"))

        events = await harness.turn(alice(), LAMP)

        kinds = [e.type for e in events]
        assert kinds.index("warning") < kinds.index("delta")

    async def test_one_that_takes_too_long_is_the_same(self) -> None:
        harness = Harness(actions=True, settings=TurnSettings(actions_timeout_s=0.05))
        harness.actions.delay_s = 5

        events = await harness.turn(alice(), LAMP)

        assert warnings(events) == [WarningCode.ACTIONS_UNAVAILABLE]
        assert said(events) == "An answer."

    async def test_the_person_is_told_it_was_not_possible_and_no_more_than_that(self) -> None:
        harness = Harness(actions=True)
        harness.actions.outcomes.append(
            ActionsUnavailableError("the actions service answered with an error (500)")
        )

        events = await harness.turn(alice(), LAMP)

        [warning] = [e for e in events if isinstance(e, TurnWarning)]
        assert warning.message == (
            "it could not be told whether the message asks for something to be done: "
            "it is answered as a question"
        )
        assert "500" not in warning.message
