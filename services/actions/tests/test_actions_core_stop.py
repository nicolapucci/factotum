"""The skill of the platform that stops what is ringing on a device (ADR 0029). A person who says
*enough* does not say whether it is a timer or an alarm, so the skill names nothing and the device,
which knows what rings, stops all of it; the answer of the device is read here, in code, and said in
a sentence of the code, in the language of the person."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import uuid4

import pytest

from factotum.actions.core import Stop, platform_skills
from factotum.actions.rig import HOME_TEXT, ActionsRig, plan_of
from factotum.actions.store import InMemoryStore
from factotum.actions.testing import START, FakeSpeech
from factotum.contracts.actions import ActStatus
from factotum.contracts.audit import ActionResult
from factotum.contracts.commands import CommandResult, StopRinging
from factotum.contracts.effects import Effect
from factotum.contracts.errors import SkillRunError
from factotum.contracts.identity import Owner, Principal, Subject
from factotum.contracts.ids import SkillId, TenantId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import SkillMode, SkillTarget
from factotum.contracts.testing import make_device_id, make_tenant_id
from factotum.contracts.timekeeping import AlarmInfo, RingsStopped, TimerInfo, data_of

KITCHEN = "Kitchen speaker"


def civil(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001 - civil time


NOW = civil(2026, 10, 13, 22, 1)
"""A Tuesday evening, on the wall of the device."""

RINGING_TIMER = TimerInfo(id="t1", seconds=60, left_s=0, ringing=True)
RINGING_ALARM = AlarmInfo(id="a2", at=civil(2026, 10, 13, 22, 1), ringing=True)


def request_of(tenant: TenantId) -> RunRequest:
    return RunRequest(
        tenant_id=tenant,
        owner=Owner.of_subject(Subject.tenant(tenant)),
        skill_id=SkillId(uuid4()),
        skill_name="stop",
        version=1,
        mode=SkillMode.INSTRUCTIONS,
        params={},
        timeout_s=10,
    )


def a_device(tenant: TenantId) -> Principal:
    return Principal(tenant_id=tenant, device_id=make_device_id(), acts_as=Subject.tenant(tenant))


def answered(data: Mapping[str, object] | None) -> Sequence[CommandResult]:
    return [CommandResult.ok(data)]  # type: ignore[arg-type]


class TestWhatItIs:
    def test_it_is_a_skill_of_the_platform_that_acts_on_the_device_that_was_spoken_to(self) -> None:
        manifest = platform_skills(FakeSpeech()).manifests()["stop"]

        assert manifest.target is SkillTarget.SYSTEM
        assert manifest.entrypoint is None
        assert manifest.permissions.network == ()
        assert manifest.permissions.secrets == ()

    def test_it_asks_for_nothing_because_it_names_nothing(self) -> None:
        manifest = platform_skills(FakeSpeech()).manifests()["stop"]

        assert manifest.parameters == {}
        assert manifest.required == ()

    def test_what_it_stops_can_be_set_again_and_that_is_all_it_changes(self) -> None:
        assert platform_skills(FakeSpeech()).manifests()["stop"].effect is Effect.REVERSIBLE

    def test_it_is_found_by_what_it_says_of_itself_in_both_languages(self) -> None:
        examples = " ".join(platform_skills(FakeSpeech()).manifests()["stop"].examples)

        assert "stop" in examples
        assert "basta" in examples

    def test_it_does_not_say_that_it_cancels_a_timer_that_has_not_rung(self) -> None:
        """That is for ``timer-cancel`` and ``alarm-cancel``: *enough* is said of a noise."""
        manifest = platform_skills(FakeSpeech()).manifests()["stop"]

        assert "none that has not rung" in manifest.description

    async def test_each_tenant_is_given_it_and_nobody_elses(self) -> None:
        store, core = InMemoryStore(), platform_skills(FakeSpeech())
        one, two = make_tenant_id(), make_tenant_id()
        await core.ensure(store, one, now=START)
        await core.ensure(store, two, now=START)

        mine = await store.list_skills(one, [Owner.of_subject(Subject.tenant(one))])
        yours = await store.list_skills(two, [Owner.of_subject(Subject.tenant(two))])

        assert "stop" in {s.name for s in mine}
        assert {s.id for s in mine if s.name == "stop"}.isdisjoint(
            {s.id for s in yours if s.name == "stop"}
        )


class TestStopping:
    async def test_the_device_is_told_and_nothing_is_named(self) -> None:
        tenant = make_tenant_id()

        done = await Stop().run(request_of(tenant), a_device(tenant), language="en")

        assert done.commands == (StopRinging(),)
        assert done.message is None  # what is said is what the device answers
        assert done.audit == "asked to stop what rings"

    @pytest.mark.parametrize(
        ("stopped", "language", "said"),
        [
            (RingsStopped(now=NOW), "en", "Nothing is ringing."),
            (RingsStopped(now=NOW), "it", "Non sta suonando niente."),
            (RingsStopped(now=NOW, timers=(RINGING_TIMER,)), "en", "Stopped."),
            (RingsStopped(now=NOW, timers=(RINGING_TIMER,)), "it", "Fermato."),
            (RingsStopped(now=NOW, alarms=(RINGING_ALARM,)), "en", "Stopped."),
            (RingsStopped(now=NOW, alarms=(RINGING_ALARM,)), "it", "Fermato."),
            (
                RingsStopped(now=NOW, timers=(RINGING_TIMER,), alarms=(RINGING_ALARM,)),
                "it",
                "Fermato.",
            ),
        ],
    )
    async def test_it_says_what_the_device_stopped_in_the_language_of_the_person(
        self, stopped: RingsStopped, language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await Stop().run(request_of(tenant), a_device(tenant), language=language)

        assert done.after is not None
        assert done.after(answered(data_of(stopped))) == said

    @pytest.mark.parametrize(
        "answer",
        [None, {}, {"volume": 40}, {"now": "tomorrow"}, {"now": NOW.isoformat(), "timers": 3}],
    )
    async def test_a_device_that_does_not_say_what_it_stopped_is_a_skill_that_failed(
        self, answer: Mapping[str, object] | None
    ) -> None:
        """Nothing is guessed: a device that answers nothing did not say that nothing rang."""
        tenant = make_tenant_id()
        done = await Stop().run(request_of(tenant), a_device(tenant), language="en")

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say what it stopped"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestFromAMessageToADevice:
    """The whole path in the service, with the answers of a device made up."""

    @staticmethod
    def rig() -> ActionsRig:
        return ActionsRig(home_text=HOME_TEXT, core=platform_skills(FakeSpeech()))

    async def test_enough_stops_what_rings_on_the_speaker_that_was_spoken_to(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(
            data_of(RingsStopped(now=NOW, alarms=(RINGING_ALARM,)))
        )
        rig.plans(plan_of("stop", language="it"))

        outcome = await rig.hear(KITCHEN, "basta")

        assert (outcome.status, outcome.reply, outcome.skill) == (
            ActStatus.DONE,
            "Fermato.",
            "stop",
        )
        assert rig.dispatcher.given == [(rig.tenant, rig.device(KITCHEN).id, StopRinging())]
        assert rig.runner.requests == []  # a skill of the platform: no sandbox
        [record] = await rig.audit()
        assert (record.skill_name, record.result, record.detail) == (
            "stop",
            ActionResult.EXECUTED,
            "asked to stop what rings",
        )

    async def test_with_nothing_ringing_the_person_is_told_so(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(data_of(RingsStopped(now=NOW)))
        rig.plans(plan_of("stop"))

        outcome = await rig.hear(KITCHEN, "stop")

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Nothing is ringing.")
