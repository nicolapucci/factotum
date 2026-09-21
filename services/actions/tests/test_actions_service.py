"""The action path from a message to an outcome, with everything around it made of doubles."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from factotum.actions.execution import DryRunRunner
from factotum.actions.rig import ActionsRig, plan_of
from factotum.actions.service import ActionSettings
from factotum.actions.store import InMemoryStore
from factotum.actions.testing import GARAGE, LAMP, ROUTER, VOLUME, WEATHER
from factotum.contracts.actions import ActCode, ActOutcome, ActRequest, ActStatus
from factotum.contracts.approval import ApprovalOutcome, ApprovalResult, ApproverKind
from factotum.contracts.audit import ActionResult, AuditRecord
from factotum.contracts.authorization import DecisionOutcome, Reason
from factotum.contracts.cards import Card
from factotum.contracts.commands import CommandResult, CommandStatus, SetVolume, ShowCard
from factotum.contracts.devices import DevicePolicy, SystemDeviceRecord
from factotum.contracts.effects import Effect
from factotum.contracts.errors import (
    GatewayUnavailableError,
    ProviderTimeoutError,
    RunnerUnavailableError,
    SkillRunError,
)
from factotum.contracts.identity import Principal, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId
from factotum.contracts.runner import RunResult
from factotum.contracts.skills import (
    ApprovalPolicy,
    ConfirmPolicy,
    ParameterSpec,
    ParamType,
    SkillManifest,
)

KITCHEN = "Kitchen speaker"
DISPLAY = "Living room display"
PLAYER = "Study player"
PHONE = "My phone"

OPEN_THE_GARAGE = plan_of(
    "garage-door",
    device="Garage door",
    parameters={"action": "open"},
    question="Do you really want me to open the garage door?",
)


class TestWhatIsNotAnAction:
    async def test_with_no_skills_a_message_is_no_action_and_no_model_is_asked(self) -> None:
        rig = ActionsRig()

        outcome = await rig.hear(KITCHEN, "what does the warranty say about water")

        assert outcome == ActOutcome.no_action()
        assert rig.chat.requests == []
        assert await rig.audit() == []

    async def test_a_message_far_from_every_skill_is_no_action_and_no_model_is_asked(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP, WEATHER)

        outcome = await rig.hear(KITCHEN, "explain photosynthesis to me slowly")

        assert outcome == ActOutcome.no_action()
        assert rig.chat.requests == []

    async def test_a_message_near_a_skill_that_the_model_says_is_not_an_action_is_no_action(
        self,
    ) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of(None, reason="asks how to do it, not to do it"))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert outcome == ActOutcome.no_action()
        assert len(rig.chat.requests) == 1
        assert rig.runner.requests == []
        assert await rig.audit() == []

    async def test_a_model_that_cannot_plan_leaves_the_message_to_be_answered_as_a_question(
        self,
    ) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(GatewayUnavailableError("the model gateway could not be reached"))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert outcome == ActOutcome.not_read()
        assert outcome.degraded
        assert rig.runner.requests == []

    @pytest.mark.parametrize(
        "what_the_model_does",
        [ProviderTimeoutError("the model did not decide in time"), {"skill": 42, "nonsense": True}],
        ids=["too-slow", "not-a-plan"],
    )
    async def test_so_is_one_that_took_too_long_or_was_answered_with_something_else(
        self, what_the_model_does: dict[str, Any] | Exception
    ) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(what_the_model_does)

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert outcome == ActOutcome.not_read()
        assert rig.runner.requests == []

    async def test_a_model_that_read_it_and_found_nothing_to_do_is_not_a_model_that_failed(
        self,
    ) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of(None, reason="a question"))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert not outcome.degraded

    async def test_a_skill_the_model_made_up_is_not_run(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of("format-the-disk", parameters={"disk": "c"}))

        assert await rig.hear(KITCHEN, "turn on the living room lamp") == ActOutcome.no_action()
        assert rig.runner.requests == []

    async def test_a_skill_that_is_installed_but_not_enabled_is_not_offered(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP, enable=False)

        assert await rig.hear(KITCHEN, "turn on the living room lamp") == ActOutcome.no_action()
        assert rig.chat.requests == []

    async def test_the_skills_of_another_tenant_are_not_offered(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        other = TenantId(uuid4())
        stranger = Principal(
            tenant_id=other, device_id=rig.device(KITCHEN).id, acts_as=Subject.tenant(other)
        )

        outcome = await rig.service.act(
            stranger, ActRequest(message="turn on the living room lamp")
        )

        assert outcome == ActOutcome.no_action()
        assert rig.chat.requests == []


class TestDoingIt:
    async def test_a_reversible_action_on_a_device_the_speaker_may_use_is_done_at_once(
        self,
    ) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(
            plan_of(
                "lamp",
                device="Living room lamp",
                parameters={"state": "on"},
                question="Do you want me to turn on the lamp?",
            )
        )

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert (outcome.status, outcome.reply, outcome.skill) == (
            ActStatus.DONE,
            "Done by the runner.",
            "lamp",
        )
        [request] = rig.runner.requests
        assert (request.skill_name, request.version) == ("lamp", 1)
        assert request.params == {"state": "on"}
        assert request.device_params == {
            "entity_id": "light.living_room",
            "host": "homeassistant.local",
        }
        assert request.permissions == LAMP.permissions
        assert request.entrypoint == "scripts/run.py"
        [record] = await rig.audit()
        assert record.id == outcome.audit_id
        assert (record.decision, record.result, record.confirmed, record.approval) == (
            DecisionOutcome.ALLOW,
            ActionResult.EXECUTED,
            None,
            None,
        )
        assert record.params == {"state": "on"}
        assert record.device_id == rig.device(KITCHEN).id
        assert record.acts_as == Subject.tenant(rig.tenant)
        assert record.skill_name == "lamp"
        assert len(record.targets) == 1

    async def test_what_the_runner_says_is_said_as_it_is_and_nothing_makes_it_up(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.runner.results["weather"] = RunResult(message="In Rome it is 21 degrees.")
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert outcome.reply == "In Rome it is 21 degrees."
        [request] = rig.runner.requests
        assert (request.params, request.device_params) == ({"place": "Rome"}, {})

    @pytest.mark.parametrize(
        ("language", "said"), [("en", "Done."), ("it", "Fatto."), ("xx", "Done.")]
    )
    async def test_a_skill_that_says_nothing_is_answered_with_a_word_in_the_language(
        self, language: str, said: str
    ) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.runner.results["weather"] = RunResult()
        rig.plans(plan_of("weather", parameters={"place": "Rome"}, language=language))

        assert (await rig.hear(KITCHEN, "what is the weather like in Rome")).reply == said

    async def test_the_most_a_skill_may_say_is_said_whole(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.runner.results["weather"] = RunResult(message="x" * 2000)
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        reply = (await rig.hear(KITCHEN, "what is the weather like in Rome")).reply

        assert reply == "x" * 2000

    async def test_a_read_only_action_is_not_asked_about(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert outcome.status is ActStatus.DONE
        assert len(rig.runner.requests) == 1

    async def test_the_values_the_model_wrote_loosely_are_what_the_skill_declares(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(
            plan_of(
                "lamp",
                device="the lamp",
                parameters={"state": "ON", "brightness": "40%", "ignored": 1},
            )
        )

        await rig.hear(KITCHEN, "turn on the living room lamp")

        assert rig.runner.requests[0].params == {"state": "on", "brightness": 40}


class TestNothingIsRunOnTheDryRun:
    async def test_it_is_said_and_it_is_audited_as_not_executed(self) -> None:
        rig = ActionsRig(runner=DryRunRunner())
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert outcome.status is ActStatus.DONE
        assert outcome.reply == "Dry run: I would have run lamp, and nothing was done."
        [record] = await rig.audit()
        assert (record.result, record.detail) == (
            ActionResult.NOT_EXECUTED,
            "dry run: nothing was run",
        )

    async def test_in_italian_when_the_person_spoke_italian(self) -> None:
        rig = ActionsRig(runner=DryRunRunner())
        await rig.install(LAMP)
        rig.plans(
            plan_of("lamp", device="Living room lamp", parameters={"state": "on"}, language="it")
        )

        outcome = await rig.hear(KITCHEN, "accendi la luce del salotto")

        assert outcome.reply == "Prova a vuoto: avrei eseguito lamp, e non è stato fatto nulla."


class TestCommandsForADevice:
    async def volume(self, rig: ActionsRig, **plan: Any) -> ActOutcome:
        await rig.install(VOLUME)
        rig.runner.results["volume"] = RunResult(
            message="Volume set to 30.", commands=(SetVolume(level=30),)
        )
        rig.plans(plan_of("volume", parameters={"level": 30}, **plan))
        return await rig.hear(KITCHEN, "set the volume to 30")

    async def test_a_command_is_given_to_the_device_that_was_spoken_to(self) -> None:
        rig = ActionsRig()

        outcome = await self.volume(rig)

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Volume set to 30.")
        assert rig.dispatcher.given == [(rig.tenant, rig.device(KITCHEN).id, SetVolume(level=30))]
        [record] = await rig.audit()
        assert record.result is ActionResult.EXECUTED

    async def test_or_to_the_one_the_skill_was_aimed_at(self) -> None:
        rig = ActionsRig()

        await self.volume(rig, device="the player")

        assert rig.dispatcher.given == [(rig.tenant, rig.device(PLAYER).id, SetVolume(level=30))]

    async def test_a_command_the_device_did_not_declare_it_can_run_is_not_given(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        card = ShowCard(card=Card(title="Rome"))
        rig.runner.results["weather"] = RunResult(message="Sunny.", commands=(card,))
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
        assert outcome.reply == "I asked the device, and it did not do it."
        assert rig.dispatcher.given == []
        [record] = await rig.audit()
        assert (record.result, record.detail) == (
            ActionResult.FAILED,
            "a command was not done (unsupported)",
        )

    async def test_a_command_a_device_did_not_do_is_a_failure(self) -> None:
        rig = ActionsRig()
        rig.dispatcher.answer = CommandResult(status=CommandStatus.OFFLINE, detail="gone")

        outcome = await self.volume(rig)

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED

    async def test_a_dispatcher_that_fails_is_a_command_that_was_not_done(self) -> None:
        rig = ActionsRig()
        rig.dispatcher.answer = GatewayUnavailableError("the edge is away")

        outcome = await self.volume(rig)

        assert outcome.code is ActCode.COMMAND_FAILED

    async def test_a_skill_asked_for_from_no_device_has_no_device_to_give_commands_to(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.runner.results["weather"] = RunResult(commands=(SetVolume(level=5),))
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(None, "what is the weather like in Rome")

        assert outcome.code is ActCode.COMMAND_FAILED
        assert rig.dispatcher.given == []


class TestWhatThePolicySays:
    async def test_a_shared_speaker_may_not_start_what_cannot_be_undone(self) -> None:
        rig = ActionsRig()
        await rig.install(GARAGE)
        rig.plans(OPEN_THE_GARAGE)

        outcome = await rig.hear(KITCHEN, "open the garage door")

        assert (outcome.status, outcome.code) == (ActStatus.REFUSED, ActCode.DENIED)
        assert outcome.reply == "This device is not allowed to do that."
        assert outcome.reasons == (Reason.EXCEEDS_DEVICE_MAX_EFFECT,)
        assert rig.runner.requests == []
        [record] = await rig.audit()
        assert (record.decision, record.result, record.reasons) == (
            DecisionOutcome.DENY,
            ActionResult.NOT_EXECUTED,
            (Reason.EXCEEDS_DEVICE_MAX_EFFECT,),
        )
        assert (
            await rig.store.get_pending(
                rig.tenant, Subject.tenant(rig.tenant), rig.device(KITCHEN).id
            )
            is None
        )

    async def test_and_says_so_in_the_language_of_the_person(self) -> None:
        rig = ActionsRig()
        await rig.install(GARAGE)
        rig.plans({**OPEN_THE_GARAGE, "language": "it"})

        outcome = await rig.hear(KITCHEN, "apri il garage")

        assert outcome.reply == "Questo dispositivo non può farlo."

    async def test_a_device_nobody_knows_is_not_one_anything_is_done_for(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))
        unknown = Principal(
            tenant_id=rig.tenant, device_id=DeviceId(uuid4()), acts_as=Subject.tenant(rig.tenant)
        )

        outcome = await rig.service.act(unknown, ActRequest(message="turn on the living room lamp"))

        assert (outcome.status, outcome.code) == (ActStatus.REFUSED, ActCode.UNKNOWN_DEVICE)
        assert outcome.reply == "I do not know this device, so I did nothing."
        assert rig.runner.requests == []
        [record] = await rig.audit()
        assert (record.decision, record.result) == (DecisionOutcome.DENY, ActionResult.NOT_EXECUTED)

    async def test_but_a_message_from_it_that_is_no_action_is_answered_like_any_other(self) -> None:
        rig = ActionsRig()
        unknown = Principal(
            tenant_id=rig.tenant, device_id=DeviceId(uuid4()), acts_as=Subject.tenant(rig.tenant)
        )

        outcome = await rig.service.act(unknown, ActRequest(message="what is the capital of Peru"))

        assert outcome == ActOutcome.no_action()

    async def test_a_device_that_wants_every_action_approved_gets_the_approval_path(self) -> None:
        home = """
devices:
  - name: Kitchen speaker
    hardware_id: kitchen-01
    token: kitchen-credential
    capabilities: [audio_in, audio_out]
    policy: {requires_approval: true}
"""
        rig = ActionsRig(home_text=home)
        await rig.install(WEATHER)
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert outcome.code is ActCode.APPROVAL_UNAVAILABLE
        assert rig.approver.requests[0].reasons == (Reason.APPROVAL_REQUIRED_BY_DEVICE,)


class TestAskingForAYes:
    async def asked(self, rig: ActionsRig, plan: dict[str, Any] | None = None) -> ActOutcome:
        await rig.install(GARAGE)
        rig.plans(plan or OPEN_THE_GARAGE)
        return await rig.hear(DISPLAY, "open the garage door")

    async def test_an_action_that_cannot_be_undone_asks_first_and_runs_nothing(self) -> None:
        rig = ActionsRig()

        outcome = await self.asked(rig)

        assert outcome.status is ActStatus.NEEDS_CONFIRMATION
        assert outcome.reply == "Do you really want me to open the garage door?"
        assert outcome.reasons == (Reason.IRREVERSIBLE_EFFECT, Reason.SKILL_CONFIRM_POLICY)
        assert rig.runner.requests == []
        [record] = await rig.audit()
        assert record.id == outcome.audit_id
        assert (record.decision, record.result, record.confirmed, record.detail) == (
            DecisionOutcome.REQUIRE_CONFIRMATION,
            ActionResult.NOT_EXECUTED,
            None,
            "asked for confirmation",
        )
        waiting = await rig.store.get_pending(
            rig.tenant, Subject.tenant(rig.tenant), rig.device(DISPLAY).id
        )
        assert waiting is not None

    async def test_without_a_question_of_the_models_it_is_asked_in_the_words_of_the_catalog(
        self,
    ) -> None:
        rig = ActionsRig()

        outcome = await self.asked(rig, {**OPEN_THE_GARAGE, "question": None, "language": "it"})

        assert outcome.reply == "Vuoi che esegua garage-door?"

    async def test_a_yes_runs_what_was_asked_and_the_trail_says_it_was_confirmed(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)

        outcome = await rig.hear(DISPLAY, "yes")

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Done by the runner.")
        [request] = rig.runner.requests
        assert (request.skill_name, request.params) == ("garage-door", {"action": "open"})
        assert request.device_params["entity_id"] == "cover.garage"
        asked, done = await rig.audit()
        assert (asked.result, asked.confirmed) == (ActionResult.NOT_EXECUTED, None)
        assert done.id == outcome.audit_id
        assert (done.decision, done.result, done.confirmed) == (
            DecisionOutcome.REQUIRE_CONFIRMATION,
            ActionResult.EXECUTED,
            True,
        )
        assert (
            await rig.store.get_pending(
                rig.tenant, Subject.tenant(rig.tenant), rig.device(DISPLAY).id
            )
            is None
        )

    async def test_a_yes_is_a_yes_once(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        await rig.hear(DISPLAY, "yes")

        again = await rig.hear(DISPLAY, "yes")

        assert again == ActOutcome.no_action()
        assert len(rig.runner.requests) == 1

    async def test_two_yeses_at_once_run_it_once(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)

        first, second = await asyncio.gather(rig.hear(DISPLAY, "yes"), rig.hear(DISPLAY, "yes"))

        assert len(rig.runner.requests) == 1
        assert sorted([first.status, second.status], key=str) == [
            ActStatus.DONE,
            ActStatus.NO_ACTION,
        ]

    @pytest.mark.parametrize("said", ["no", "Don't!", "annulla"])
    async def test_a_no_cancels_and_nothing_is_run(self, said: str) -> None:
        rig = ActionsRig()
        await self.asked(rig)

        outcome = await rig.hear(DISPLAY, said)

        assert (outcome.status, outcome.code) == (ActStatus.CANCELLED, ActCode.DECLINED)
        assert outcome.reply == "Okay, I will not do it."
        assert rig.runner.requests == []
        _, declined = await rig.audit()
        assert (declined.result, declined.confirmed, declined.detail) == (
            ActionResult.NOT_EXECUTED,
            False,
            "the person declined",
        )

    async def test_and_in_the_language_of_the_question(self) -> None:
        rig = ActionsRig()
        await self.asked(rig, {**OPEN_THE_GARAGE, "language": "it"})

        outcome = await rig.hear(DISPLAY, "no")

        assert outcome.reply == "Va bene, non lo faccio."

    async def test_a_yes_in_words_the_lexicon_does_not_have_is_read_by_the_model(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        rig.plans({"reason": "agrees", "answer": "yes"})

        outcome = await rig.hear(DISPLAY, "why not, go for it then")

        assert outcome.status is ActStatus.DONE
        assert len(rig.runner.requests) == 1

    async def test_something_else_drops_the_question_and_is_taken_as_a_new_message(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        await rig.install(WEATHER)
        rig.plans(
            {"reason": "asks something else", "answer": "other"},
            plan_of("weather", parameters={"place": "Rome"}),
        )

        outcome = await rig.hear(DISPLAY, "what is the weather like in Rome")

        assert outcome.status is ActStatus.DONE
        assert [r.skill_name for r in rig.runner.requests] == ["weather"]
        asked, lapsed, done = await rig.audit()
        assert (lapsed.skill_name, lapsed.result, lapsed.confirmed) == (
            "garage-door",
            ActionResult.NOT_EXECUTED,
            False,
        )
        assert lapsed.detail == "something else was said instead"
        assert done.skill_name == "weather"
        assert asked.detail == "asked for confirmation"

    async def test_a_message_that_is_no_action_after_a_dropped_question_is_no_action(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        rig.plans({"reason": "asks something else", "answer": "other"})

        outcome = await rig.hear(DISPLAY, "explain photosynthesis to me slowly")

        assert outcome == ActOutcome.no_action()
        assert rig.runner.requests == []

    async def test_a_model_that_cannot_read_the_answer_is_no_yes(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        rig.plans(GatewayUnavailableError("the model gateway could not be reached"))

        outcome = await rig.hear(DISPLAY, "hmm, alright then")

        assert outcome == ActOutcome.no_action()
        assert rig.runner.requests == []

    async def test_a_yes_that_comes_too_late_is_no_yes(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        rig.clock.advance(seconds=61)

        outcome = await rig.hear(DISPLAY, "yes")

        assert outcome == ActOutcome.no_action()
        assert rig.runner.requests == []
        _, lapsed = await rig.audit()
        assert lapsed.detail == "the confirmation expired"
        assert lapsed.confirmed is False

    async def test_how_long_a_yes_is_waited_for_is_a_setting(self) -> None:
        rig = ActionsRig(settings=ActionSettings(confirmation_ttl=timedelta(minutes=5)))
        await self.asked(rig)
        rig.clock.advance(minutes=4)

        assert (await rig.hear(DISPLAY, "yes")).status is ActStatus.DONE

    async def test_a_yes_on_another_device_confirms_nothing(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)

        outcome = await rig.hear(KITCHEN, "yes")

        assert outcome == ActOutcome.no_action()
        assert rig.runner.requests == []
        assert (await rig.hear(DISPLAY, "yes")).status is ActStatus.DONE

    async def test_a_yes_in_another_tenant_confirms_nothing(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        other = TenantId(uuid4())
        stranger = Principal(
            tenant_id=other, device_id=rig.device(DISPLAY).id, acts_as=Subject.tenant(other)
        )

        outcome = await rig.service.act(stranger, ActRequest(message="yes"))

        assert outcome == ActOutcome.no_action()
        assert rig.runner.requests == []

    async def test_a_skill_that_was_switched_off_since_is_not_run_on_a_yes(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        [skill] = await rig.store.list_skills(rig.tenant, [rig.shared_owner])
        await rig.store.disable(rig.tenant, skill.id)

        outcome = await rig.hear(DISPLAY, "yes")

        assert (outcome.status, outcome.code) == (ActStatus.REFUSED, ActCode.SKILL_NOT_ENABLED)
        assert outcome.reply == "That is not available any more."
        assert rig.runner.requests == []
        _, refused = await rig.audit()
        assert (refused.result, refused.confirmed) == (ActionResult.NOT_EXECUTED, True)

    async def test_a_skill_that_was_changed_since_is_not_run_on_a_yes_to_what_it_was(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        changed = GARAGE.model_copy(update={"description": "Open or close the big door."})
        await rig.install(changed)
        [skill] = await rig.store.list_skills(rig.tenant, [rig.shared_owner])
        await rig.store.enable(
            rig.tenant, skill.id, 2, granted=changed.permissions, by=None, now=rig.clock()
        )

        outcome = await rig.hear(DISPLAY, "yes")

        assert outcome.code is ActCode.SKILL_NOT_ENABLED
        assert rig.runner.requests == []

    async def test_a_device_whose_policy_was_tightened_since_refuses_the_yes(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)

        class Tightened:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            async def system_device(
                self, tenant_id: TenantId, device_id: DeviceId
            ) -> SystemDeviceRecord | None:
                record: SystemDeviceRecord | None = await self._inner.system_device(
                    tenant_id, device_id
                )
                if record is not None and record.name == DISPLAY:
                    return record.model_copy(
                        update={"policy": DevicePolicy(max_effect=Effect.REVERSIBLE)}
                    )
                return record

            async def system_devices(self, principal: Principal) -> Any:
                return await self._inner.system_devices(principal)

            async def bound_devices(self, principal: Principal, skill_name: str) -> Any:
                return await self._inner.bound_devices(principal, skill_name)

        rig.use_directory(Tightened(rig.directory))

        outcome = await rig.hear(DISPLAY, "yes")

        assert (outcome.status, outcome.code) == (ActStatus.REFUSED, ActCode.DENIED)
        assert rig.runner.requests == []
        _, refused = await rig.audit()
        assert (refused.decision, refused.confirmed) == (DecisionOutcome.DENY, True)

    async def test_a_confirmation_that_cannot_be_read_any_more_is_dropped(self) -> None:
        rig = ActionsRig()
        await self.asked(rig)
        waiting = await rig.store.get_pending(
            rig.tenant, Subject.tenant(rig.tenant), rig.device(DISPLAY).id
        )
        assert waiting is not None
        await rig.store.put_pending(replace(waiting, proposal={"unrecognized": True}))

        outcome = await rig.hear(DISPLAY, "yes")

        assert outcome == ActOutcome.no_action()
        assert rig.runner.requests == []


class TestWhatAnUntrustedTurnMayNotDo:
    async def test_words_from_a_document_or_a_page_make_an_action_ask_first(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp", untrusted=True)

        assert outcome.status is ActStatus.NEEDS_CONFIRMATION
        assert outcome.reasons == (Reason.UNTRUSTED_CONTENT_IN_TURN,)
        assert rig.runner.requests == []

    async def test_and_the_yes_that_follows_runs_it_and_the_trail_says_why_it_was_asked(
        self,
    ) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))
        await rig.hear(KITCHEN, "turn on the living room lamp", untrusted=True)

        outcome = await rig.hear(KITCHEN, "yes")

        assert outcome.status is ActStatus.DONE
        asked, done = await rig.audit()
        assert asked.reasons == (Reason.UNTRUSTED_CONTENT_IN_TURN,)
        assert (done.result, done.confirmed) == (ActionResult.EXECUTED, True)

    async def test_what_only_reads_is_not_held_up_by_it(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome", untrusted=True)

        assert outcome.status is ActStatus.DONE

    async def test_a_skill_that_always_asks_asks_even_to_read(self) -> None:
        rig = ActionsRig()
        careful = WEATHER.model_copy(update={"confirm": ConfirmPolicy.ALWAYS})
        await rig.install(careful)
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert outcome.status is ActStatus.NEEDS_CONFIRMATION
        assert outcome.reasons == (Reason.SKILL_CONFIRM_POLICY,)


class TestApproval:
    async def test_with_no_way_to_ask_an_action_that_needs_approval_is_refused(self) -> None:
        rig = ActionsRig()
        await rig.install(ROUTER)
        rig.plans(plan_of("restart-router"))

        outcome = await rig.hear(KITCHEN, "restart the router")

        assert (outcome.status, outcome.code) == (ActStatus.REFUSED, ActCode.APPROVAL_UNAVAILABLE)
        assert outcome.reply == "That needs somebody's approval, and I have no way to ask for it."
        assert rig.runner.requests == []
        [record] = await rig.audit()
        assert (record.decision, record.result) == (
            DecisionOutcome.REQUIRE_APPROVAL,
            ActionResult.NOT_EXECUTED,
        )
        assert record.approval is not None
        assert (record.approval.outcome, record.approval.approved_by) == (
            ApprovalOutcome.UNAVAILABLE,
            None,
        )

    async def test_the_approver_is_told_what_is_asked_and_who_may_say_yes(self) -> None:
        rig = ActionsRig()
        await rig.install(ROUTER)
        rig.plans(plan_of("restart-router"), plan_of("restart-router"))

        await rig.hear(KITCHEN, "restart the router")
        await rig.hear(PHONE, "restart the router")

        for_household, for_person = rig.approver.requests
        assert for_household.approvers.kind is ApproverKind.TENANT_ADMINS
        assert (for_person.approvers.kind, for_person.approvers.user_id) == (
            ApproverKind.USER,
            rig.user,
        )
        assert for_household.action.skill_name == "restart-router"
        assert for_household.risk is Effect.REVERSIBLE
        assert for_household.reasons == (Reason.APPROVAL_REQUIRED_BY_SKILL,)
        assert for_household.device_id == rig.device(KITCHEN).id
        assert for_household.expires_at - for_household.requested_at == timedelta(minutes=10)

    async def test_what_is_asked_of_the_approver_has_its_secrets_masked(self) -> None:
        rig = ActionsRig()
        careful = SkillManifest(
            name="restart-router",
            description=ROUTER.description,
            examples=ROUTER.examples,
            parameters={"api_token": ParameterSpec(type=ParamType.STRING)},
            required=("api_token",),
            effect=Effect.REVERSIBLE,
            approval=ApprovalPolicy.REQUIRED,
            entrypoint="scripts/run.py",
        )
        await rig.install(careful)
        rig.plans(plan_of("restart-router", parameters={"api_token": "sk-a-very-secret-value"}))

        await rig.hear(KITCHEN, "restart the router")

        assert rig.approver.requests[0].action.params == {"api_token": "[REDACTED]"}
        [record] = await rig.audit()
        assert record.params == {"api_token": "[REDACTED]"}

    async def test_an_approved_action_runs_and_the_trail_says_who_approved_it(self) -> None:
        rig = ActionsRig()
        await rig.install(ROUTER)
        admin = UserId(uuid4())
        rig.approver.answer = ApprovalResult.approved(admin)
        rig.plans(plan_of("restart-router"))

        outcome = await rig.hear(KITCHEN, "restart the router")

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Done by the runner.")
        [request] = rig.runner.requests
        assert request.skill_name == "restart-router"
        [record] = await rig.audit()
        assert (record.decision, record.result, record.confirmed) == (
            DecisionOutcome.REQUIRE_APPROVAL,
            ActionResult.EXECUTED,
            None,
        )
        assert record.approval is not None
        assert (record.approval.outcome, record.approval.approved_by) == (
            ApprovalOutcome.APPROVED,
            admin,
        )

    @pytest.mark.parametrize(
        ("answer", "code", "said"),
        [
            (
                ApprovalResult(outcome=ApprovalOutcome.DENIED),
                ActCode.APPROVAL_DENIED,
                "It was not approved.",
            ),
            (
                ApprovalResult(outcome=ApprovalOutcome.EXPIRED),
                ActCode.APPROVAL_EXPIRED,
                "Nobody approved it in time.",
            ),
            (
                GatewayUnavailableError("the mechanism is away"),
                ActCode.APPROVAL_UNAVAILABLE,
                "That needs somebody's approval, and I have no way to ask for it.",
            ),
        ],
    )
    async def test_an_action_that_is_not_approved_is_not_run(
        self, answer: Any, code: ActCode, said: str
    ) -> None:
        rig = ActionsRig()
        await rig.install(ROUTER)
        rig.approver.answer = answer
        rig.plans(plan_of("restart-router"))

        outcome = await rig.hear(KITCHEN, "restart the router")

        assert (outcome.status, outcome.code, outcome.reply) == (ActStatus.REFUSED, code, said)
        assert rig.runner.requests == []

    async def test_an_approver_that_takes_too_long_is_an_approval_that_did_not_come(self) -> None:
        rig = ActionsRig(settings=ActionSettings(approval_wait_s=0.05))
        await rig.install(ROUTER)
        rig.approver.delay_s = 5
        rig.plans(plan_of("restart-router"))

        outcome = await rig.hear(KITCHEN, "restart the router")

        assert outcome.code is ActCode.APPROVAL_EXPIRED
        assert rig.runner.requests == []
        [record] = await rig.audit()
        assert record.approval is not None
        assert record.approval.outcome is ApprovalOutcome.EXPIRED

    async def test_a_yes_is_not_an_approval(self) -> None:
        rig = ActionsRig()
        await rig.install(ROUTER)
        rig.plans(plan_of("restart-router"))
        await rig.hear(KITCHEN, "restart the router")

        outcome = await rig.hear(KITCHEN, "yes")

        assert outcome == ActOutcome.no_action()
        assert rig.runner.requests == []


class TestWhenSomethingIsMissing:
    async def test_which_one_is_asked_when_there_are_two_and_none_is_named(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", parameters={"state": "on"}))

        outcome = await rig.hear(PHONE, "turn on the lamp")

        assert (outcome.status, outcome.code) == (ActStatus.NEEDS_MORE, ActCode.AMBIGUOUS_DEVICE)
        assert outcome.reply == "Which one do you mean: Bedroom lamp, Living room lamp?"
        assert rig.runner.requests == []
        assert await rig.audit() == []

    async def test_a_value_that_is_missing_is_asked_for_in_the_words_of_the_model(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.plans(plan_of("weather", ask="Which city do you want the weather for?"))

        outcome = await rig.hear(KITCHEN, "what is the weather like")

        assert (outcome.status, outcome.code) == (ActStatus.NEEDS_MORE, ActCode.MISSING_VALUES)
        assert outcome.reply == "Which city do you want the weather for?"
        assert rig.runner.requests == []

    async def test_nothing_waits_for_the_answer_the_person_says_it_all_again(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.plans(plan_of("weather"), plan_of("weather", parameters={"place": "Rome"}))

        await rig.hear(KITCHEN, "what is the weather like")
        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert outcome.status is ActStatus.DONE
        assert (
            await rig.store.get_pending(
                rig.tenant, Subject.tenant(rig.tenant), rig.device(KITCHEN).id
            )
            is None
        )


class TestWhenARunGoesWrong:
    @pytest.mark.parametrize(
        ("failure", "code", "said"),
        [
            (SkillRunError("the script failed"), ActCode.SKILL_FAILED, "It did not work."),
            (
                RunnerUnavailableError("nothing runs skills"),
                ActCode.RUNNER_UNAVAILABLE,
                "I cannot run that right now.",
            ),
            (RuntimeError("a bug in a runner"), ActCode.SKILL_FAILED, "It did not work."),
        ],
    )
    async def test_it_is_a_failure_that_is_said_and_audited(
        self, failure: Exception, code: ActCode, said: str
    ) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.runner.results["weather"] = failure
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert (outcome.status, outcome.code, outcome.reply) == (ActStatus.FAILED, code, said)
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED
        assert record.id == outcome.audit_id

    async def test_a_skill_that_takes_too_long_is_a_failure(self) -> None:
        rig = ActionsRig(settings=ActionSettings(run_margin_s=0.05))
        await rig.install(WEATHER.model_copy(update={"timeout_s": 1}))
        rig.runner.delay_s = 5
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
        [record] = await rig.audit()
        assert record.detail == "the skill failed: TimeoutError"

    async def test_a_trail_that_cannot_be_written_does_not_change_what_the_person_is_told(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class NoTrail(InMemoryStore):
            async def add_audit(self, record: AuditRecord) -> None:
                raise OSError("the disk is full")

        rig = ActionsRig(store=NoTrail())
        await rig.install(WEATHER)
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert (outcome.status, outcome.audit_id) == (ActStatus.DONE, None)
        assert "an action could not be audited" in caplog.text


class TestWhoseTrailItIs:
    async def test_the_trail_is_of_the_tenant_that_acted_and_of_nobody_else(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))
        await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert len(await rig.audit()) == 1
        assert await rig.store.list_audit(TenantId(uuid4()), before=None, limit=10) == []

    async def test_a_record_names_the_device_and_the_subject_and_has_nobody_who_spoke(self) -> None:
        rig = ActionsRig()
        await rig.install(WEATHER)
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))
        await rig.hear(PHONE, "what is the weather like in Rome")

        [record] = await rig.audit()

        assert record.device_id == rig.device(PHONE).id
        assert record.acts_as == Subject.user(rig.user)
        assert "speaker" not in record.model_dump()
