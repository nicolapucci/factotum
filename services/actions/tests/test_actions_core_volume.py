"""The skills of the platform for the sound of a device (ADR 0029): the volume set, turned up or
down, muted, and said. Each is the interface between a model and the speaker of a device: it passes
on what was said, tells the device no number of its own, and says what the device answered in a
sentence of the code, in the language of the person."""

from collections.abc import Sequence
from typing import ClassVar
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.core import (
    CoreResult,
    CoreSkill,
    CoreSkills,
    Mute,
    Volume,
    VolumeChange,
    VolumeStatus,
    platform_skills,
)
from factotum.actions.core.phrases import LANGUAGES, CoreSay, core_say
from factotum.actions.rig import HOME_TEXT, ActionsRig, plan_of
from factotum.actions.store import InMemoryStore
from factotum.actions.testing import START, FakeSpeech, just_announce
from factotum.contracts.actions import ActCode, ActStatus
from factotum.contracts.audit import ActionResult
from factotum.contracts.commands import (
    AdjustVolume,
    CommandResult,
    CommandStatus,
    GetState,
    SetMute,
    SetVolume,
    VolumeDirection,
)
from factotum.contracts.effects import Effect
from factotum.contracts.errors import SkillRunError
from factotum.contracts.identity import Owner, Principal, Subject
from factotum.contracts.ids import SkillId, TenantId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import ParamValue, SkillMode, SkillTarget
from factotum.contracts.testing import make_device_id, make_tenant_id

KITCHEN = "Kitchen speaker"
DISPLAY = "Living room display"
UP, DOWN = VolumeDirection.UP, VolumeDirection.DOWN

HOME = HOME_TEXT.replace(
    "bindings:",
    """\
  - name: Doorbell
    hardware_id: doorbell-01
    token: doorbell-credential
    owner: tenant
    capabilities: [audio_in, led]
bindings:""",
)
"""The home of the rig, with a device that has a microphone and no speaker."""


def request_of(tenant: TenantId, skill: str, **params: ParamValue) -> RunRequest:
    return RunRequest(
        tenant_id=tenant,
        owner=Owner.of_subject(Subject.tenant(tenant)),
        skill_id=SkillId(uuid4()),
        skill_name=skill,
        version=1,
        mode=SkillMode.INSTRUCTIONS,
        params=params,
        timeout_s=10,
    )


def a_device(tenant: TenantId) -> Principal:
    return Principal(tenant_id=tenant, device_id=make_device_id(), acts_as=Subject.tenant(tenant))


def results(*, data: dict[str, object] | None = None) -> list[CommandResult]:
    return [CommandResult.ok(data)]  # type: ignore[arg-type]


class TestWhatTheyAre:
    def test_they_are_skills_of_the_platform_that_act_on_the_device_that_was_spoken_to(
        self,
    ) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        for name in ("volume", "volume-change", "mute", "volume-status"):
            assert manifests[name].target is SkillTarget.SYSTEM, name
            assert manifests[name].entrypoint is None, name  # no script: they are code
            assert manifests[name].permissions.network == (), name  # and reach nothing
            assert manifests[name].permissions.secrets == (), name

    def test_what_they_change_can_be_undone_and_asking_how_it_is_changes_nothing(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        assert manifests["volume"].effect is Effect.REVERSIBLE
        assert manifests["volume-change"].effect is Effect.REVERSIBLE
        assert manifests["mute"].effect is Effect.REVERSIBLE
        assert manifests["volume-status"].effect is Effect.READ_ONLY

    def test_each_asks_for_what_it_cannot_do_without_and_nothing_else(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        assert manifests["volume"].required == ("level",)
        assert manifests["volume-change"].required == ("direction",)
        assert set(manifests["volume-change"].parameters) == {"direction", "amount"}
        assert manifests["mute"].required == ("muted",)
        assert manifests["volume-status"].parameters == {}

    def test_they_are_found_by_what_they_say_of_themselves_in_both_languages(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        for name in ("volume", "volume-change", "mute", "volume-status"):
            examples = " ".join(manifests[name].examples)
            assert any(word in examples for word in ("the", "it", "volume", "sound", "mute")), name
            assert any(word in examples for word in ("il ", "muto", "volume", "più")), name

    def test_each_tenant_is_given_all_of_them_and_nobody_elses(self) -> None:
        async def check() -> None:
            store, core = InMemoryStore(), platform_skills(FakeSpeech())
            one, two = make_tenant_id(), make_tenant_id()
            await core.ensure(store, one, now=START)
            await core.ensure(store, two, now=START)

            mine = await store.list_skills(one, [Owner.of_subject(Subject.tenant(one))])
            yours = await store.list_skills(two, [Owner.of_subject(Subject.tenant(two))])

            assert sorted(s.name for s in mine) == sorted(s.name for s in yours)
            assert {"announce", "volume", "volume-change", "mute", "volume-status"} <= {
                s.name for s in mine
            }
            assert {s.id for s in mine}.isdisjoint({s.id for s in yours})

        import asyncio

        asyncio.run(check())


class TestTheLevelOfTheVolume:
    @given(st.integers(0, 100))
    async def test_a_level_that_was_said_is_given_to_the_device_as_it_is(self, level: int) -> None:
        tenant = make_tenant_id()

        done = await Volume().run(
            request_of(tenant, "volume", level=level), a_device(tenant), language="en"
        )

        assert done.commands == (SetVolume(level=level),)
        assert done.message == f"Volume at {level} percent."

    async def test_and_it_is_said_in_italian_to_a_person_who_spoke_it(self) -> None:
        tenant = make_tenant_id()

        done = await Volume().run(
            request_of(tenant, "volume", level=30), a_device(tenant), language="it"
        )

        assert done.message == "Volume al 30 per cento."
        assert done.audit == "volume set to 30"

    @pytest.mark.parametrize("level", [None, -1, 101, 3.5, "30", True])
    async def test_what_is_not_a_level_is_not_given_to_a_device(self, level: ParamValue) -> None:
        tenant = make_tenant_id()
        request = request_of(tenant, "volume", **({} if level is None else {"level": level}))

        with pytest.raises(SkillRunError, match="a level from 0 to 100"):
            await Volume().run(request, a_device(tenant), language="en")


class TestTurningTheVolume:
    @pytest.mark.parametrize("direction", [UP, DOWN])
    async def test_what_was_said_is_given_to_the_device_and_no_step_of_the_skills(
        self, direction: VolumeDirection
    ) -> None:
        tenant = make_tenant_id()

        done = await VolumeChange().run(
            request_of(tenant, "volume-change", direction=direction.value),
            a_device(tenant),
            language="en",
        )

        assert done.commands == (AdjustVolume(direction=direction),)
        assert isinstance(done.commands[0], AdjustVolume)
        assert done.commands[0].amount is None
        assert done.audit == f"volume turned {direction.value} by its own step"

    async def test_an_amount_that_was_said_is_given_too(self) -> None:
        tenant = make_tenant_id()

        done = await VolumeChange().run(
            request_of(tenant, "volume-change", direction="down", amount=20),
            a_device(tenant),
            language="en",
        )

        assert done.commands == (AdjustVolume(direction=DOWN, amount=20),)
        assert done.audit == "volume turned down by 20"

    @pytest.mark.parametrize("direction", [None, "", "louder", "UP", 1, True])
    async def test_a_direction_that_is_not_up_or_down_is_not_given_to_a_device(
        self, direction: ParamValue | None
    ) -> None:
        tenant = make_tenant_id()
        params: dict[str, ParamValue] = {} if direction is None else {"direction": direction}

        with pytest.raises(SkillRunError, match="up or down"):
            await VolumeChange().run(
                request_of(tenant, "volume-change", **params), a_device(tenant), language="en"
            )

    @pytest.mark.parametrize("amount", [0, -5, 101, 2.5, "ten", True])
    async def test_an_amount_that_is_not_from_1_to_100_points_is_not_given_either(
        self, amount: ParamValue
    ) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillRunError, match="1 to 100 points"):
            await VolumeChange().run(
                request_of(tenant, "volume-change", direction="up", amount=amount),
                a_device(tenant),
                language="en",
            )

    @pytest.mark.parametrize(
        ("language", "said"), [("en", "Volume at 60 percent."), ("it", "Volume al 60 per cento.")]
    )
    async def test_it_says_where_the_volume_came_to_when_the_device_says(
        self, language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await VolumeChange().run(
            request_of(tenant, "volume-change", direction="up"), a_device(tenant), language=language
        )

        assert done.message is None
        assert done.after is not None
        assert done.after(results(data={"volume": 60})) == said

    @pytest.mark.parametrize(
        "answer", [None, {}, {"volume": "loud"}, {"volume": True}, {"muted": True}]
    )
    async def test_and_leaves_the_usual_word_for_a_thing_done_when_it_does_not(
        self, answer: dict[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await VolumeChange().run(
            request_of(tenant, "volume-change", direction="up"), a_device(tenant), language="en"
        )

        assert done.after is not None
        assert done.after(results(data=answer)) is None
        assert done.after([]) is None


class TestMutingTheSound:
    @pytest.mark.parametrize(
        ("muted", "language", "said"),
        [
            (True, "en", "Muted."),
            (False, "en", "The sound is back."),
            (True, "it", "Audio in muto."),
            (False, "it", "Audio riattivato."),
        ],
    )
    async def test_it_is_muted_or_brought_back_as_it_was_said(
        self, muted: bool, language: str, said: str
    ) -> None:
        tenant = make_tenant_id()

        done = await Mute().run(
            request_of(tenant, "mute", muted=muted), a_device(tenant), language=language
        )

        assert done.commands == (SetMute(muted=muted),)
        assert done.message == said
        assert done.audit == ("sound muted" if muted else "sound brought back")

    @pytest.mark.parametrize("muted", [None, "true", 1, 0, "mute"])
    async def test_what_is_not_a_yes_or_a_no_is_not_given_to_a_device(
        self, muted: ParamValue | None
    ) -> None:
        tenant = make_tenant_id()
        params: dict[str, ParamValue] = {} if muted is None else {"muted": muted}

        with pytest.raises(SkillRunError, match="muted or it is not"):
            await Mute().run(request_of(tenant, "mute", **params), a_device(tenant), language="en")


class TestSayingHowItIs:
    async def test_the_device_is_asked_and_nothing_is_changed(self) -> None:
        tenant = make_tenant_id()

        done = await VolumeStatus().run(
            request_of(tenant, "volume-status"), a_device(tenant), language="en"
        )

        assert done.commands == (GetState(),)
        assert done.message is None
        assert done.audit == "asked how loud the device is"

    @pytest.mark.parametrize(
        ("answer", "language", "said"),
        [
            ({"volume": 45, "muted": False}, "en", "The volume is at 45 percent."),
            (
                {"volume": 45, "muted": True},
                "en",
                "The sound is muted. The volume is at 45 percent.",
            ),
            ({"volume": 45, "muted": False}, "it", "Il volume è al 45 per cento."),
            ({"volume": 0, "muted": True}, "it", "L'audio è in muto. Il volume è al 0 per cento."),
        ],
    )
    async def test_what_the_device_answered_is_said_in_the_language_of_the_person(
        self, answer: dict[str, object], language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await VolumeStatus().run(
            request_of(tenant, "volume-status"), a_device(tenant), language=language
        )

        assert done.after is not None
        assert done.after(results(data=answer)) == said

    @pytest.mark.parametrize(
        "answer",
        [
            None,
            {},
            {"volume": 40},
            {"muted": False},
            {"volume": "40", "muted": False},
            {"volume": 40, "muted": "no"},
        ],
    )
    async def test_a_device_that_does_not_say_how_it_is_is_a_skill_that_failed_and_not_a_guess(
        self, answer: dict[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await VolumeStatus().run(
            request_of(tenant, "volume-status"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say how loud"):
            done.after(results(data=answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestTheSentences:
    def test_each_is_in_each_language(self) -> None:
        assert {"en", "it"} == LANGUAGES
        every_field: dict[str, str | int] = dict.fromkeys(
            ("level", "duration", "left", "count", "when", "at", "list", "time", "date"), 50
        )
        for language in LANGUAGES:
            for key in CoreSay:
                said = core_say(language, key, **every_field)
                assert said, (language, key)
                assert "{" not in said, (language, key)

    def test_a_language_there_are_no_words_for_is_answered_in_english(self) -> None:
        assert core_say("fr", CoreSay.MUTED) == core_say("en", CoreSay.MUTED) == "Muted."
        assert core_say(None, CoreSay.MUTED) == "Muted."

    def test_the_level_is_in_the_sentence_that_says_one(self) -> None:
        for language in LANGUAGES:
            for key in (CoreSay.VOLUME_AT, CoreSay.SOUND_AT, CoreSay.SOUND_MUTED):
                assert "73" in core_say(language, key, level=73), (language, key)


class Both(CoreSkill):
    """A skill of the platform that has a sentence of its own and reads what the device answered."""

    source: ClassVar[str] = """\
---
name: echo
description: Bring the sound back on the device that is being spoken to.
examples:
  - echo
target: system
effect: reversible
---
A skill for a test.
"""

    def __init__(self, answered: str | None) -> None:
        super().__init__()
        self._answered = answered

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        def after(results: Sequence[CommandResult]) -> str | None:
            return self._answered

        return CoreResult(
            message="what the skill says of itself", commands=(SetMute(muted=False),), after=after
        )


class TestWhatTheSkillSays:
    @staticmethod
    async def reply(answered: str | None) -> str | None:
        rig = ActionsRig(
            home_text=HOME, core=CoreSkills(Both(answered)), every_skill_is_offered=True
        )
        rig.plans(plan_of("echo"))

        outcome = await rig.hear(KITCHEN, "echo")

        assert outcome.status is ActStatus.DONE
        return outcome.reply

    async def test_what_the_device_answered_is_said_in_place_of_the_sentence_of_the_skill(
        self,
    ) -> None:
        assert await self.reply("from what the device answered") == "from what the device answered"

    async def test_and_the_sentence_of_the_skill_is_said_when_the_answer_says_nothing(self) -> None:
        assert await self.reply(None) == "what the skill says of itself"


class TestFromAMessageToADevice:
    """The whole path in the service, with the answers of a device made up."""

    @staticmethod
    def rig() -> ActionsRig:
        return ActionsRig(home_text=HOME, core=platform_skills(FakeSpeech()))

    async def test_a_level_reaches_the_speaker_that_was_spoken_to_and_nothing_ran_in_a_sandbox(
        self,
    ) -> None:
        rig = self.rig()
        rig.plans(plan_of("volume", parameters={"level": 30}))

        outcome = await rig.hear(KITCHEN, "set the volume to 30")

        assert (outcome.status, outcome.reply, outcome.skill) == (
            ActStatus.DONE,
            "Volume at 30 percent.",
            "volume",
        )
        assert rig.dispatcher.given == [(rig.tenant, rig.device(KITCHEN).id, SetVolume(level=30))]
        assert rig.runner.requests == []
        [record] = await rig.audit()
        assert (record.skill_name, record.result, record.detail) == (
            "volume",
            ActionResult.EXECUTED,
            "volume set to 30",
        )

    async def test_turned_up_with_no_amount_the_device_is_told_none_and_says_where_it_came_to(
        self,
    ) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok({"volume": 60})
        rig.plans(plan_of("volume-change", parameters={"direction": "up"}, language="it"))

        outcome = await rig.hear(KITCHEN, "alza il volume")

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Volume al 60 per cento.")
        [(_, _, command)] = rig.dispatcher.given
        assert command == AdjustVolume(direction=UP)
        assert isinstance(command, AdjustVolume)
        assert command.amount is None

    async def test_a_device_that_does_not_say_where_it_came_to_leaves_the_usual_word(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("volume-change", parameters={"direction": "down", "amount": 5}))

        outcome = await rig.hear(KITCHEN, "turn the volume down by five")

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Done.")
        [(_, _, command)] = rig.dispatcher.given
        assert command == AdjustVolume(direction=DOWN, amount=5)

    async def test_the_sound_is_muted_on_the_device_that_was_named(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("mute", device=DISPLAY, parameters={"muted": True}))

        outcome = await rig.hear(KITCHEN, "mute the living room display")

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Muted.")
        assert rig.dispatcher.given == [(rig.tenant, rig.device(DISPLAY).id, SetMute(muted=True))]

    async def test_how_it_is_is_asked_of_the_device_and_said_from_what_it_answered(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok({"volume": 40, "muted": True})
        rig.plans(plan_of("volume-status", language="it"))

        outcome = await rig.hear(KITCHEN, "sei in muto")

        assert (outcome.status, outcome.reply) == (
            ActStatus.DONE,
            "L'audio è in muto. Il volume è al 40 per cento.",
        )
        assert rig.dispatcher.given == [(rig.tenant, rig.device(KITCHEN).id, GetState())]
        [record] = await rig.audit()
        assert (record.result, record.detail) == (
            ActionResult.EXECUTED,
            "asked how loud the device is",
        )

    async def test_a_device_that_answers_with_nothing_to_read_is_a_skill_that_failed(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("volume-status"))  # the device answers ok and says nothing

        outcome = await rig.hear(KITCHEN, "what is the volume")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
        assert outcome.reply == "The volume-status skill failed."
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED
        assert "did not say how loud" in (record.detail or "")

    async def test_a_command_that_the_device_did_not_do_is_said_with_the_name_of_the_skill(
        self,
    ) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult(status=CommandStatus.FAILED, detail="amplifier off")
        rig.plans(plan_of("mute", parameters={"muted": True}))

        outcome = await rig.hear(KITCHEN, "mute")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
        assert "mute" in (outcome.reply or "")

    async def test_a_device_with_no_speaker_is_told_nothing_and_the_skill_failed(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("volume", device="Doorbell", parameters={"level": 30}))

        outcome = await rig.hear(KITCHEN, "set the volume of the doorbell to 30")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
        assert rig.dispatcher.given == []

    async def test_a_level_that_the_message_does_not_say_is_asked_for_and_nothing_is_given(
        self,
    ) -> None:
        rig = self.rig()
        rig.plans(plan_of("volume", parameters={"level": 100}))

        outcome = await rig.hear(KITCHEN, "set the volume")

        assert (outcome.status, outcome.code) == (ActStatus.NEEDS_MORE, ActCode.MISSING_VALUES)
        assert rig.dispatcher.given == []

    async def test_a_level_in_words_and_the_ends_of_the_range_are_levels_that_were_said(
        self,
    ) -> None:
        # What is looked at is what the code makes of a level, and not which skill the stand-in of
        # the embedding model finds near a message (*al massimo* is nearer to the other skill).
        rig = ActionsRig(
            home_text=HOME, core=platform_skills(FakeSpeech()), every_skill_is_offered=True
        )
        rig.plans(
            plan_of("volume", parameters={"level": 50}),
            plan_of("volume", parameters={"level": 100}),
        )

        await rig.hear(KITCHEN, "metti il volume a cinquanta")
        await rig.hear(KITCHEN, "metti il volume al massimo")

        assert [command for _, _, command in rig.dispatcher.given] == [
            SetVolume(level=50),
            SetVolume(level=100),
        ]

    async def test_two_tenants_each_speak_to_their_own_device(self) -> None:
        store, speech = InMemoryStore(), FakeSpeech()
        core = platform_skills(speech)
        one = ActionsRig(home_text=HOME, store=store, core=core)
        two = ActionsRig(home_text=HOME, store=store, core=core)
        one.plans(plan_of("volume", parameters={"level": 20}))
        two.plans(plan_of("volume", parameters={"level": 80}))

        await one.hear(KITCHEN, "set the volume to 20")
        await two.hear(KITCHEN, "set the volume to 80")

        assert one.dispatcher.given == [(one.tenant, one.device(KITCHEN).id, SetVolume(level=20))]
        assert two.dispatcher.given == [(two.tenant, two.device(KITCHEN).id, SetVolume(level=80))]
        assert len(await one.audit()) == len(await two.audit()) == 1

    async def test_the_announcement_is_still_there_beside_them(self) -> None:
        assert just_announce().names == ("announce",)
        assert platform_skills(FakeSpeech()).names == (
            "announce",
            "volume",
            "volume-change",
            "mute",
            "volume-status",
            "timer",
            "timer-cancel",
            "timer-status",
            "alarm",
            "alarm-cancel",
            "alarm-postpone",
            "alarm-change",
            "alarm-status",
            "stop",
            "clock",
        )
