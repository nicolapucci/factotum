"""A skill of the platform from a message to an outcome (ADR 0026): ``announce`` is installed for a
tenant when it is first asked for, chosen as any skill is, authorized, confirmed and audited as any
is, and run in this process: the voice of the message is made by the gateway and the device is
given the clips to play. What is not the platform's (a skill of a person of the same name) is run
in the sandbox as ever."""

from typing import Any

from factotum.actions.core import platform_skills
from factotum.actions.execution import DryRunRunner
from factotum.actions.rig import HOME_TEXT, ActionsRig, plan_of
from factotum.actions.store import InMemoryStore
from factotum.actions.testing import ANNOUNCE, START, FakeSpeech
from factotum.contracts.actions import ActCode, ActOutcome, ActStatus
from factotum.contracts.audio import AudioPayload
from factotum.contracts.audit import ActionResult
from factotum.contracts.authorization import DecisionOutcome
from factotum.contracts.commands import CommandResult, CommandStatus, PlayAudio
from factotum.contracts.errors import GatewayUnavailableError
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import DeviceId, TenantId

KITCHEN = "Kitchen speaker"
DISPLAY = "Living room display"
PHONE = "My phone"
TABLET = "Hall tablet"
MESSAGE = "Dinner is ready"
TELL = "tell the living room that dinner is ready"

HOME = HOME_TEXT.replace(
    "bindings:",
    """\
  - name: Doorbell
    hardware_id: doorbell-01
    token: doorbell-credential
    owner: tenant
    capabilities: [audio_in, led]
  - name: Hall tablet
    hardware_id: hall-01
    token: hall-credential
    owner: tenant
    capabilities: [audio_in, audio_out]
    policy: {max_effect: read_only}
bindings:""",
)
"""The home of the rig, with a device that has no speaker and one that may only read."""


def announce_plan(message: str = MESSAGE, **fields: Any) -> dict[str, Any]:
    return plan_of("announce", parameters={"message": message}, **fields)


def a_rig(**kwargs: Any) -> tuple[ActionsRig, FakeSpeech]:
    """A service given the skills of the platform, with a voice that is made up."""
    speech = FakeSpeech()
    return ActionsRig(home_text=HOME, core=platform_skills(speech), **kwargs), speech


def a_message(sentences: int) -> str:
    return " ".join(f"Sentence number {n} says that dinner is ready." for n in range(sentences))


class TestAnnouncing:
    async def test_a_message_is_said_on_the_device_that_was_named(self) -> None:
        rig, _ = a_rig()
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert (outcome.status, outcome.reply, outcome.skill) == (
            ActStatus.DONE,
            "Done.",
            "announce",
        )
        assert rig.dispatcher.given == [
            (rig.tenant, rig.device(DISPLAY).id, PlayAudio(text=MESSAGE, audio=clip(MESSAGE)))
        ]

    async def test_or_on_the_one_that_was_spoken_to_when_none_was_named(self) -> None:
        rig, _ = a_rig()
        rig.plans(announce_plan())

        await rig.hear(KITCHEN, "say that dinner is ready")

        [(tenant, device, command)] = rig.dispatcher.given
        assert (tenant, device) == (rig.tenant, rig.device(KITCHEN).id)
        assert command == PlayAudio(text=MESSAGE, audio=clip(MESSAGE))

    async def test_a_long_message_is_given_in_clips_that_say_all_of_it_in_order(self) -> None:
        rig, _ = a_rig()
        message = a_message(9)  # what a person says is at most 500 characters
        rig.plans(announce_plan(message, device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert outcome.status is ActStatus.DONE
        said = [FakeSpeech.words_in(command.audio) for _, _, command in played(rig)]
        assert len(said) == 2
        assert " ".join(said) == message
        assert {device for _, device, _ in rig.dispatcher.given} == {rig.device(DISPLAY).id}

    async def test_it_is_said_in_the_language_of_the_person_who_asked(self) -> None:
        rig, _ = a_rig()
        rig.plans(announce_plan("la cena è pronta", device=DISPLAY, language="it"))

        outcome = await rig.hear(KITCHEN, "annuncia in salotto che la cena è pronta")

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Fatto.")
        assert [command.text for _, _, command in played(rig)] == ["la cena è pronta"]

    async def test_what_a_person_says_in_one_message_is_a_phrase_and_no_more_than_that(
        self,
    ) -> None:
        rig, speech = a_rig()
        rig.plans(announce_plan("dinner is ready " * 40, device=DISPLAY))  # 600 characters

        outcome = await rig.hear(KITCHEN, TELL)

        assert (outcome.status, outcome.code) == (ActStatus.NEEDS_MORE, ActCode.MISSING_VALUES)
        assert speech.asked == []
        assert rig.dispatcher.given == []

    async def test_nothing_was_run_in_the_sandbox_and_no_model_wrote_a_plan(self) -> None:
        rig, _ = a_rig(instructions=True)  # a service that can write the plan of a skill
        rig.plans(announce_plan(device=DISPLAY))

        await rig.hear(KITCHEN, TELL)

        assert rig.runner.requests == []
        assert len(rig.chat.asked) == 1  # the reading of the message, and nothing after it

    async def test_it_is_said_though_there_is_no_runner_at_all(self) -> None:
        rig, _ = a_rig(runner=DryRunRunner())
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Done.")  # not a dry run
        assert len(played(rig)) == 1

    async def test_a_service_that_was_not_given_the_skills_of_the_platform_has_none(self) -> None:
        rig = ActionsRig(home_text=HOME)
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert outcome == ActOutcome.no_skill()
        assert rig.dispatcher.given == []


class TestFoundByMeaning:
    """A skill of the platform is measurable from the first message: it is indexed as the tenant is
    given it, or it would be offered to a model unscored, and a model asked, for every message until
    an index that is made in the background caught up."""

    async def test_a_message_far_from_what_the_platform_does_asks_no_model_even_the_first_time(
        self,
    ) -> None:
        rig, speech = a_rig()

        outcome = await rig.hear(KITCHEN, "explain photosynthesis to me slowly")

        assert outcome == ActOutcome.no_skill()
        assert rig.chat.requests == []
        assert speech.asked == []

    async def test_the_skill_is_indexed_when_the_tenant_is_given_it(self) -> None:
        rig, _ = a_rig()

        await rig.hear(KITCHEN, "explain photosynthesis to me slowly")

        [announce] = await rig.store.list_skills(rig.tenant, [rig.shared_owner])
        indexed = await rig.store.indexed(rig.tenant, rig.shared_owner, rig.embedder.space_id)
        assert indexed == {announce.id}

    async def test_a_message_near_it_is_offered_to_the_model_by_meaning_the_first_time(
        self,
    ) -> None:
        rig, _ = a_rig()
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, "tell the living room that dinner is ready")

        assert (outcome.status, outcome.skill) == (ActStatus.DONE, "announce")
        assert len(rig.chat.requests) == 1  # asked once, for a message that is near it


class TestWhoseVoice:
    async def test_the_voice_of_a_shared_device_is_the_tenants(self) -> None:
        rig, speech = a_rig()
        rig.plans(announce_plan(device=DISPLAY))

        await rig.hear(KITCHEN, TELL)

        assert speech.asked == [(Owner.of_subject(Subject.tenant(rig.tenant)), MESSAGE)]

    async def test_the_voice_of_a_persons_own_device_is_theirs(self) -> None:
        rig, speech = a_rig()
        rig.plans(announce_plan(device=DISPLAY))

        await rig.hear(PHONE, TELL)

        assert speech.asked == [(Owner.of_subject(Subject.user(rig.user)), MESSAGE)]

    async def test_two_tenants_each_have_their_own_skill_their_own_voice_and_their_own_trail(
        self,
    ) -> None:
        speech, store = FakeSpeech(), InMemoryStore()
        core = platform_skills(speech)
        one = ActionsRig(home_text=HOME, store=store, core=core)
        two = ActionsRig(home_text=HOME, store=store, core=core)
        one.plans(announce_plan("Dinner is ready", device=DISPLAY))
        two.plans(announce_plan("Lunch is ready", device=DISPLAY))

        await one.hear(KITCHEN, TELL)
        await two.hear(KITCHEN, TELL)

        assert speech.asked == [
            (one.shared_owner, "Dinner is ready"),
            (two.shared_owner, "Lunch is ready"),
        ]
        assert [(t, d) for t, d, _ in one.dispatcher.given] == [
            (one.tenant, one.device(DISPLAY).id)
        ]
        assert [(t, d) for t, d, _ in two.dispatcher.given] == [
            (two.tenant, two.device(DISPLAY).id)
        ]
        [mine] = await store.list_skills(one.tenant, [one.shared_owner])
        [yours] = await store.list_skills(two.tenant, [two.shared_owner])
        assert mine.id != yours.id
        assert await store.list_skills(two.tenant, [one.shared_owner]) == []
        assert len(await one.audit()) == len(await two.audit()) == 1


class TestWhatTheTrailSays:
    async def test_it_says_what_was_said_where_and_that_it_was_done(self) -> None:
        rig, _ = a_rig()
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        [record] = await rig.audit()
        assert record.id == outcome.audit_id
        assert record.skill_name == "announce"
        assert record.targets == (rig.device(DISPLAY).id,)
        assert record.params == {"message": MESSAGE}
        assert (record.decision, record.result, record.confirmed) == (
            DecisionOutcome.ALLOW,
            ActionResult.EXECUTED,
            None,
        )
        assert record.detail == "said 'Dinner is ready' in 1 clip"

    async def test_it_holds_the_words_and_never_the_sound(self) -> None:
        rig, _ = a_rig()
        rig.plans(announce_plan(a_message(9), device=DISPLAY))

        await rig.hear(KITCHEN, TELL)

        [record] = await rig.audit()
        assert record.detail is not None
        assert record.detail.startswith("said 'Sentence number 0 says")
        assert len(record.model_dump_json()) < 2000  # a clip is a megabyte, and it is not here


class TestAuthorizedAsAnySkillIs:
    async def test_a_device_that_may_only_read_may_not_announce_and_no_voice_is_made(self) -> None:
        rig, speech = a_rig()
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(TABLET, TELL)

        assert (outcome.status, outcome.code) == (ActStatus.REFUSED, ActCode.DENIED)
        assert speech.asked == []
        assert rig.dispatcher.given == []
        [record] = await rig.audit()
        assert (record.decision, record.result) == (DecisionOutcome.DENY, ActionResult.NOT_EXECUTED)

    async def test_a_turn_that_carried_somebody_elses_words_asks_before_it_says_anything(
        self,
    ) -> None:
        rig, speech = a_rig()
        rig.plans(announce_plan(device=DISPLAY))

        asked = await rig.hear(KITCHEN, TELL, untrusted=True)

        assert asked.status is ActStatus.NEEDS_CONFIRMATION
        assert speech.asked == []
        assert rig.dispatcher.given == []

        done = await rig.hear(KITCHEN, "yes")

        assert (done.status, done.reply) == (ActStatus.DONE, "Done.")
        assert [command for _, _, command in rig.dispatcher.given] == [
            PlayAudio(text=MESSAGE, audio=clip(MESSAGE))
        ]
        asked_record, done_record = await rig.audit()
        assert (asked_record.result, done_record.result, done_record.confirmed) == (
            ActionResult.NOT_EXECUTED,
            ActionResult.EXECUTED,
            True,
        )
        assert done_record.detail == "said 'Dinner is ready' in 1 clip"

    async def test_one_that_an_administrator_disabled_is_not_a_skill_that_is_there(self) -> None:
        rig, speech = a_rig()
        core = rig.core
        assert core is not None
        await core.ensure(rig.store, rig.tenant, now=START)
        [skill] = await rig.store.list_skills(rig.tenant, [rig.shared_owner])
        await rig.store.disable(rig.tenant, skill.id)
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert outcome == ActOutcome.no_skill()
        assert speech.asked == []


class TestWhenItCannotBeDone:
    async def test_a_voice_that_cannot_be_made_is_a_skill_that_failed_and_says_what_kind(
        self,
    ) -> None:
        rig, speech = a_rig()
        speech.failures = [GatewayUnavailableError("http://10.0.0.7:8080 refused the key sk-abc")]
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
        assert outcome.reply == "The announce skill failed."
        assert rig.dispatcher.given == []
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED
        assert (
            record.detail == "the skill failed: the voice could not be made (gateway_unavailable)"
        )
        assert "sk-abc" not in record.model_dump_json() + outcome.model_dump_json()

    async def test_a_device_with_no_speaker_is_not_given_a_clip(self) -> None:
        rig, _ = a_rig()
        rig.plans(announce_plan(device="Doorbell"))

        outcome = await rig.hear(KITCHEN, "tell the doorbell that dinner is ready")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
        assert rig.dispatcher.given == []
        [record] = await rig.audit()
        assert record.detail == "a command was not done (unsupported)"

    async def test_a_device_that_does_not_take_the_clip_is_a_command_that_was_not_done(
        self,
    ) -> None:
        rig, _ = a_rig()
        rig.dispatcher.answer = CommandResult(
            status=CommandStatus.FAILED, detail="there is too much waiting to be said"
        )
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
        assert (
            outcome.reply == "I asked the device to run the announce skill, and it did not do it."
        )

    async def test_a_second_clip_that_is_refused_is_a_command_that_was_not_done(self) -> None:
        rig, _ = a_rig()
        rig.dispatcher.answers = [
            CommandResult.ok(),
            CommandResult(status=CommandStatus.FAILED, detail="there is too much waiting"),
        ]
        rig.plans(announce_plan(a_message(9), device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert outcome.code is ActCode.COMMAND_FAILED
        assert len(played(rig)) == 2

    async def test_the_clip_after_one_that_is_refused_is_not_given(self) -> None:
        rig, _ = a_rig()
        rig.dispatcher.answers = [CommandResult(status=CommandStatus.FAILED, detail="too much")]
        rig.plans(announce_plan(a_message(9), device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert outcome.code is ActCode.COMMAND_FAILED
        assert len(played(rig)) == 1  # the second half of a message is not said without the first


class TestWhatIsNotTheirs:
    async def test_a_skill_of_a_person_that_is_called_announce_is_run_in_the_sandbox(self) -> None:
        speech = FakeSpeech()
        core = platform_skills(speech)
        rig = ActionsRig(home_text=HOME, core=core)
        await core.ensure(rig.store, rig.tenant, now=START)
        theirs = ANNOUNCE.model_copy(
            update={"entrypoint": "scripts/run.py", "description": "Shout something."}
        )
        await rig.install(theirs)  # another text: another version, which an administrator enabled
        rig.plans(announce_plan(device=DISPLAY))

        outcome = await rig.hear(KITCHEN, TELL)

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Done by the runner.")
        [request] = rig.runner.requests
        assert request.skill_name == "announce"
        assert speech.asked == []  # the platform's skill did not run: this is theirs
        assert rig.dispatcher.given == []


def clip(text: str) -> AudioPayload:
    """The clip that the voice made up here makes of a text."""
    return FakeSpeech.clip_of(text)


def played(rig: ActionsRig) -> list[tuple[TenantId, DeviceId, PlayAudio]]:
    """The clips that were given to devices, in the order they were given."""
    return [
        (tenant, device, command)
        for tenant, device, command in rig.dispatcher.given
        if isinstance(command, PlayAudio)
    ]
