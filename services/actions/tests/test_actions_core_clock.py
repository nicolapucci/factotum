"""The skill of the platform that says what time it is and what day, by the clock of a device (ADR
0029). Nobody but the device knows what time it is where it stands, so the skill asks the device
and says what its wall shows, in a sentence of the code in the language of the person: a model
never says the time and never reads it."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import uuid4

import pytest

from factotum.actions.core import Clock, platform_skills
from factotum.actions.core.phrases import (
    date_sentence,
    say_date,
    say_time,
    time_sentence,
)
from factotum.actions.rig import HOME_TEXT, ActionsRig, plan_of
from factotum.actions.store import InMemoryStore
from factotum.actions.testing import START, FakeSpeech
from factotum.contracts.actions import ActStatus
from factotum.contracts.audit import ActionResult
from factotum.contracts.commands import CommandResult, GetState
from factotum.contracts.effects import Effect
from factotum.contracts.errors import SkillRunError
from factotum.contracts.identity import Owner, Principal, Subject
from factotum.contracts.ids import SkillId, TenantId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import ParamType, ParamValue, SkillMode, SkillTarget
from factotum.contracts.testing import make_device_id, make_tenant_id
from factotum.contracts.timekeeping import AlarmInfo, TimerInfo, TimeState, data_of

KITCHEN = "Kitchen speaker"


def civil(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001 - civil time


NOW = civil(2026, 10, 14, 14, 32)
"""A Wednesday afternoon, on the wall of the device."""


def request_of(tenant: TenantId, **params: ParamValue) -> RunRequest:
    return RunRequest(
        tenant_id=tenant,
        owner=Owner.of_subject(Subject.tenant(tenant)),
        skill_id=SkillId(uuid4()),
        skill_name="clock",
        version=1,
        mode=SkillMode.INSTRUCTIONS,
        params=params,
        timeout_s=10,
    )


def a_device(tenant: TenantId) -> Principal:
    return Principal(tenant_id=tenant, device_id=make_device_id(), acts_as=Subject.tenant(tenant))


def answered(data: Mapping[str, object] | None) -> Sequence[CommandResult]:
    return [CommandResult.ok(data)]  # type: ignore[arg-type]


def shown(moment: datetime) -> Sequence[CommandResult]:
    """What a device that has a volume and keeps time answers to *how are you*."""
    return answered({"volume": 40, "muted": False, **data_of(TimeState(now=moment))})


class TestWhatItIs:
    def test_it_is_a_skill_of_the_platform_that_asks_the_device_that_was_spoken_to(self) -> None:
        manifest = platform_skills(FakeSpeech()).manifests()["clock"]

        assert manifest.target is SkillTarget.SYSTEM
        assert manifest.entrypoint is None
        assert manifest.permissions.network == ()
        assert manifest.permissions.secrets == ()

    def test_it_only_reads_so_nothing_is_asked_before_it_is_done(self) -> None:
        assert platform_skills(FakeSpeech()).manifests()["clock"].effect is Effect.READ_ONLY

    def test_it_can_be_asked_for_the_time_the_day_or_both_and_none_is_required(self) -> None:
        manifest = platform_skills(FakeSpeech()).manifests()["clock"]

        assert list(manifest.parameters) == ["about"]
        assert manifest.parameters["about"].type is ParamType.STRING
        assert manifest.parameters["about"].enum == ("time", "date", "both")
        assert manifest.required == ()

    def test_it_is_found_by_what_it_says_of_itself_in_both_languages(self) -> None:
        examples = " ".join(platform_skills(FakeSpeech()).manifests()["clock"].examples)

        for words in ("what time is it", "what day is it", "che ore sono", "che giorno è"):
            assert words in examples

    def test_it_does_not_say_that_it_knows_another_place_or_another_day(self) -> None:
        manifest = platform_skills(FakeSpeech()).manifests()["clock"]

        assert "another place" in manifest.description
        assert "another date" in manifest.description

    async def test_each_tenant_is_given_it_and_nobody_elses(self) -> None:
        store, core = InMemoryStore(), platform_skills(FakeSpeech())
        one, two = make_tenant_id(), make_tenant_id()
        await core.ensure(store, one, now=START)
        await core.ensure(store, two, now=START)

        mine = await store.list_skills(one, [Owner.of_subject(Subject.tenant(one))])
        yours = await store.list_skills(two, [Owner.of_subject(Subject.tenant(two))])

        assert "clock" in {s.name for s in mine}
        assert {s.id for s in mine if s.name == "clock"}.isdisjoint(
            {s.id for s in yours if s.name == "clock"}
        )


class TestSayingTheTime:
    @pytest.mark.parametrize(
        ("hour", "minute", "english", "italian"),
        [
            (14, 32, "2:32 PM", "14 e 32"),
            (14, 0, "2 PM", "14"),
            (9, 5, "9:05 AM", "9 e 5"),
            (0, 0, "midnight", "mezzanotte"),
            (0, 5, "12:05 AM", "mezzanotte e 5"),
            (12, 0, "noon", "mezzogiorno"),
            (12, 15, "12:15 PM", "mezzogiorno e 15"),
            (1, 0, "1 AM", "l'una"),
            (1, 5, "1:05 AM", "l'una e 5"),
            (13, 5, "1:05 PM", "13 e 5"),
            (23, 59, "11:59 PM", "23 e 59"),
        ],
    )
    def test_it_is_said_the_way_each_language_says_it(
        self, hour: int, minute: int, english: str, italian: str
    ) -> None:
        assert say_time("en", hour, minute) == english
        assert say_time("it", hour, minute) == italian

    def test_a_language_there_are_no_words_for_is_said_in_english(self) -> None:
        assert say_time("fr", 14, 32) == "2:32 PM"
        assert say_time(None, 14, 32) == "2:32 PM"

    @pytest.mark.parametrize(
        ("moment", "english", "italian"),
        [
            (NOW, "It is 2:32 PM.", "Sono le 14 e 32."),
            (civil(2026, 10, 14, 0, 0), "It is midnight.", "È mezzanotte."),
            (civil(2026, 10, 14, 0, 5), "It is 12:05 AM.", "È mezzanotte e 5."),
            (civil(2026, 10, 14, 12, 0), "It is noon.", "È mezzogiorno."),
            (civil(2026, 10, 14, 1, 5), "It is 1:05 AM.", "È l'una e 5."),
            (civil(2026, 10, 14, 13, 5), "It is 1:05 PM.", "Sono le 13 e 5."),
            (civil(2026, 10, 14, 2, 0), "It is 2 AM.", "Sono le 2."),
        ],
    )
    def test_it_is_a_sentence_in_the_singular_where_italian_says_it_so(
        self, moment: datetime, english: str, italian: str
    ) -> None:
        assert time_sentence("en", moment) == english
        assert time_sentence("it", moment) == italian


class TestSayingTheDay:
    @pytest.mark.parametrize(
        ("moment", "english", "italian"),
        [
            (NOW, "Wednesday, October 14, 2026", "mercoledì 14 ottobre 2026"),
            (civil(2026, 10, 1), "Thursday, October 1, 2026", "giovedì primo ottobre 2026"),
            (civil(2026, 10, 2), "Friday, October 2, 2026", "venerdì 2 ottobre 2026"),
            (civil(2026, 12, 31), "Thursday, December 31, 2026", "giovedì 31 dicembre 2026"),
            (civil(2027, 1, 3), "Sunday, January 3, 2027", "domenica 3 gennaio 2027"),
            (civil(2028, 2, 29), "Tuesday, February 29, 2028", "martedì 29 febbraio 2028"),
        ],
    )
    def test_it_has_the_name_of_the_day_and_the_year(
        self, moment: datetime, english: str, italian: str
    ) -> None:
        assert say_date("en", moment) == english
        assert say_date("it", moment) == italian

    def test_it_is_a_sentence(self) -> None:
        assert date_sentence("en", NOW) == "Today is Wednesday, October 14, 2026."
        assert date_sentence("it", NOW) == "Oggi è mercoledì 14 ottobre 2026."

    def test_a_language_there_are_no_words_for_is_said_in_english(self) -> None:
        assert date_sentence("fr", NOW) == "Today is Wednesday, October 14, 2026."


class TestAsking:
    @pytest.mark.parametrize(
        ("params", "audit"),
        [
            ({}, "asked the device what time and day it is"),
            ({"about": "both"}, "asked the device what time and day it is"),
            ({"about": "time"}, "asked the device what time it is"),
            ({"about": "date"}, "asked the device what day it is"),
        ],
    )
    async def test_the_device_is_asked_how_it_is_and_nothing_is_changed(
        self, params: dict[str, ParamValue], audit: str
    ) -> None:
        tenant = make_tenant_id()

        done = await Clock().run(request_of(tenant, **params), a_device(tenant), language="en")

        assert done.commands == (GetState(),)
        assert done.message is None  # what is said is what the device answers
        assert done.audit == audit

    @pytest.mark.parametrize("about", ["year", "", "TIME", 3, True])
    async def test_what_is_asked_of_it_is_the_time_or_the_date_and_nothing_else(
        self, about: ParamValue
    ) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillRunError, match="the time, the date or both"):
            await Clock().run(request_of(tenant, about=about), a_device(tenant), language="en")


class TestSaying:
    @pytest.mark.parametrize(
        ("params", "language", "said"),
        [
            ({"about": "time"}, "en", "It is 2:32 PM."),
            ({"about": "time"}, "it", "Sono le 14 e 32."),
            ({"about": "date"}, "en", "Today is Wednesday, October 14, 2026."),
            ({"about": "date"}, "it", "Oggi è mercoledì 14 ottobre 2026."),
            ({}, "en", "It is 2:32 PM. Today is Wednesday, October 14, 2026."),
            ({}, "it", "Sono le 14 e 32. Oggi è mercoledì 14 ottobre 2026."),
            ({"about": "both"}, "en", "It is 2:32 PM. Today is Wednesday, October 14, 2026."),
            ({"about": "both"}, "it", "Sono le 14 e 32. Oggi è mercoledì 14 ottobre 2026."),
        ],
    )
    async def test_it_says_what_the_wall_of_the_device_shows_in_the_language_of_the_person(
        self, params: dict[str, ParamValue], language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await Clock().run(request_of(tenant, **params), a_device(tenant), language=language)

        assert done.after is not None
        assert done.after(shown(NOW)) == said

    async def test_it_says_the_hour_the_device_shows_and_not_one_of_its_own(self) -> None:
        tenant = make_tenant_id()
        done = await Clock().run(request_of(tenant, about="time"), a_device(tenant), language="it")

        assert done.after is not None
        assert done.after(shown(civil(2026, 10, 14, 1, 5))) == "È l'una e 5."
        assert done.after(shown(civil(2026, 10, 14, 23, 59))) == "Sono le 23 e 59."

    async def test_a_device_that_holds_timers_and_alarms_is_read_for_its_wall_only(self) -> None:
        tenant = make_tenant_id()
        busy = TimeState(
            now=NOW,
            timers=(TimerInfo(id="t1", seconds=300, left_s=274),),
            alarms=(AlarmInfo(id="a1", at=civil(2026, 10, 15, 7)),),
        )
        done = await Clock().run(request_of(tenant, about="time"), a_device(tenant), language="en")

        assert done.after is not None
        assert done.after(answered(data_of(busy))) == "It is 2:32 PM."

    async def test_a_device_that_says_only_its_wall_is_enough(self) -> None:
        tenant = make_tenant_id()
        done = await Clock().run(request_of(tenant), a_device(tenant), language="en")

        assert done.after is not None
        assert done.after(answered({"now": NOW.isoformat()})) == (
            "It is 2:32 PM. Today is Wednesday, October 14, 2026."
        )

    @pytest.mark.parametrize(
        "answer",
        [
            None,
            {},
            {"volume": 40, "muted": False},
            {"now": "tomorrow"},
            {"now": "2026-10-14T14:32:00+02:00"},
            {"now": 1760445120},
        ],
    )
    async def test_a_device_that_does_not_say_what_time_it_is_is_a_skill_that_failed(
        self, answer: Mapping[str, object] | None
    ) -> None:
        """Nothing is guessed, and the platform has no clock to say instead: a wall that has a zone
        is not the civil time of the place (ADR 0029)."""
        tenant = make_tenant_id()
        done = await Clock().run(request_of(tenant), a_device(tenant), language="en")

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say what time it is"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestFromAMessageToADevice:
    """The whole path in the service, with the answers of a device made up."""

    @staticmethod
    def rig(moment: datetime = NOW) -> ActionsRig:
        rig = ActionsRig(home_text=HOME_TEXT, core=platform_skills(FakeSpeech()))
        rig.dispatcher.answer = CommandResult.ok(
            {"volume": 40, "muted": False, **data_of(TimeState(now=moment))}
        )
        return rig

    async def test_asked_what_time_it_is_the_speaker_is_asked_and_the_time_is_said(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("clock", parameters={"about": "time"}, language="it"))

        outcome = await rig.hear(KITCHEN, "che ore sono")

        assert (outcome.status, outcome.reply, outcome.skill) == (
            ActStatus.DONE,
            "Sono le 14 e 32.",
            "clock",
        )
        assert rig.dispatcher.given == [(rig.tenant, rig.device(KITCHEN).id, GetState())]
        assert rig.runner.requests == []  # a skill of the platform: no sandbox
        [record] = await rig.audit()
        assert (record.skill_name, record.result, record.detail) == (
            "clock",
            ActionResult.EXECUTED,
            "asked the device what time it is",
        )

    async def test_asked_what_day_it_is_the_day_is_said_and_not_the_time(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("clock", parameters={"about": "date"}))

        outcome = await rig.hear(KITCHEN, "what day is it today")

        assert outcome.reply == "Today is Wednesday, October 14, 2026."

    async def test_asked_for_neither_in_particular_both_are_said(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("clock"))

        outcome = await rig.hear(KITCHEN, "what time and what day is it")

        assert outcome.reply == "It is 2:32 PM. Today is Wednesday, October 14, 2026."

    async def test_the_time_is_the_one_the_device_showed_and_never_one_of_the_platform(
        self,
    ) -> None:
        rig = self.rig(civil(2031, 3, 9, 6, 45))
        rig.plans(plan_of("clock"))

        outcome = await rig.hear(KITCHEN, "what time and what day is it")

        assert outcome.reply == "It is 6:45 AM. Today is Sunday, March 9, 2031."

    async def test_a_device_that_does_not_answer_is_a_skill_that_failed_and_nobody_is_told_a_time(
        self,
    ) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok()  # accepted, and nothing to read
        rig.plans(plan_of("clock", parameters={"about": "time"}))

        outcome = await rig.hear(KITCHEN, "what time is it")

        assert outcome.status is ActStatus.FAILED
        assert outcome.reply is not None
        assert "PM" not in outcome.reply
        assert "AM" not in outcome.reply

    async def test_a_value_that_is_a_choice_is_not_looked_for_in_the_words_of_the_message(
        self,
    ) -> None:
        """*Che ore sono* does not have the word *time* in it: a choice is the model's reading."""
        rig = self.rig()
        rig.plans(plan_of("clock", parameters={"about": "time"}, language="it"))

        outcome = await rig.hear(KITCHEN, "che ore sono")

        assert outcome.status is ActStatus.DONE

    async def test_two_tenants_each_ask_their_own_device(self) -> None:
        store, speech = InMemoryStore(), FakeSpeech()
        core = platform_skills(speech)
        one = ActionsRig(home_text=HOME_TEXT, store=store, core=core)
        two = ActionsRig(home_text=HOME_TEXT, store=store, core=core)
        one.dispatcher.answer = CommandResult.ok(data_of(TimeState(now=civil(2026, 10, 14, 7, 0))))
        two.dispatcher.answer = CommandResult.ok(data_of(TimeState(now=civil(2026, 10, 14, 21, 0))))
        one.plans(plan_of("clock", parameters={"about": "time"}))
        two.plans(plan_of("clock", parameters={"about": "time"}))

        first = await one.hear(KITCHEN, "what time is it")
        second = await two.hear(KITCHEN, "what time is it")

        assert (first.reply, second.reply) == ("It is 7 AM.", "It is 9 PM.")
        assert one.dispatcher.given == [(one.tenant, one.device(KITCHEN).id, GetState())]
        assert two.dispatcher.given == [(two.tenant, two.device(KITCHEN).id, GetState())]
        assert len(await one.audit()) == len(await two.audit()) == 1
