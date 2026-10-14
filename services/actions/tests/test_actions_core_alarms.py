"""The skills of the platform for the alarms of a device (ADR 0029): one is set, cancelled, and
asked about. Each is the interface between a model and the clock and the calendar of a device: it
passes on the hour and the minute that code reads in the words of the person and the day as they
said it (never a date: the platform has no clock), and says what the device answered in a sentence
of the code, in the language of the person."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import uuid4

import pytest

from factotum.actions.core import (
    Alarm,
    AlarmCancel,
    AlarmChange,
    AlarmPostpone,
    AlarmStatus,
    platform_skills,
)
from factotum.actions.core.phrases import CoreSay, core_say
from factotum.actions.rig import HOME_TEXT, ActionsRig, plan_of
from factotum.actions.store import InMemoryStore
from factotum.actions.testing import START, FakeSpeech
from factotum.contracts.actions import ActCode, ActStatus
from factotum.contracts.audit import ActionResult
from factotum.contracts.commands import (
    AlarmDay,
    CancelAlarm,
    CommandResult,
    GetState,
    MoveAlarm,
    PostponeAlarm,
    SetAlarm,
    Weekday,
)
from factotum.contracts.effects import Effect
from factotum.contracts.errors import GatewayUnavailableError, SkillFailedError, SkillRunError
from factotum.contracts.identity import Owner, Principal, Role, Subject
from factotum.contracts.ids import SkillId, TenantId, UserId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import ParamValue, SkillMode, SkillTarget
from factotum.contracts.testing import make_device_id, make_tenant_id
from factotum.contracts.timekeeping import (
    AlarmInfo,
    AlarmsCancelled,
    AlarmSet,
    AlarmsMoved,
    AlarmsPostponed,
    TimeState,
    data_of,
)

KITCHEN = "Kitchen speaker"
ROOM_FOR_ALL = ("alarm", "alarm-cancel", "alarm-postpone", "alarm-change", "alarm-status")


def civil(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001 - civil time


NOW = civil(2026, 10, 13, 22, 1)
"""A Tuesday evening, on the wall of the device."""

TOMORROW_AT_SEVEN = civil(2026, 10, 14, 7, 0)


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


def alarm(at: datetime, *, ringing: bool = False) -> AlarmInfo:
    return AlarmInfo(id=f"a{at.hour}{at.minute}", at=at, ringing=ringing)


def answered(data: Mapping[str, object] | None) -> Sequence[CommandResult]:
    return [CommandResult.ok(data)]  # type: ignore[arg-type]


def set_to(at: datetime) -> Sequence[CommandResult]:
    return answered(data_of(AlarmSet(now=NOW, alarm=alarm(at))))


def holding(*alarms: AlarmInfo) -> Sequence[CommandResult]:
    return answered({"volume": 40, **data_of(TimeState(now=NOW, alarms=alarms))})


class TestWhatTheyAre:
    def test_they_are_skills_of_the_platform_that_act_on_the_device_that_was_spoken_to(
        self,
    ) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        for name in ROOM_FOR_ALL:
            assert manifests[name].target is SkillTarget.SYSTEM, name
            assert manifests[name].entrypoint is None, name
            assert manifests[name].permissions.network == (), name
            assert manifests[name].permissions.secrets == (), name

    def test_what_they_change_can_be_undone_and_asking_changes_nothing(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        assert manifests["alarm"].effect is Effect.REVERSIBLE
        assert manifests["alarm-cancel"].effect is Effect.REVERSIBLE
        assert manifests["alarm-postpone"].effect is Effect.REVERSIBLE
        assert manifests["alarm-change"].effect is Effect.REVERSIBLE
        assert manifests["alarm-status"].effect is Effect.READ_ONLY

    def test_each_asks_for_what_it_cannot_do_without_and_nothing_else(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        assert manifests["alarm"].required == ("time",)
        assert set(manifests["alarm"].parameters) == {"time", "day"}  # and the day only if said
        assert manifests["alarm-cancel"].required == ()
        assert set(manifests["alarm-cancel"].parameters) == {"time"}
        assert manifests["alarm-postpone"].required == ()  # how much is the device's, if not said
        assert set(manifests["alarm-postpone"].parameters) == {"by"}
        assert manifests["alarm-change"].required == ("time",)
        assert set(manifests["alarm-change"].parameters) == {"time", "day", "from"}
        assert manifests["alarm-status"].parameters == {}

    def test_the_hour_and_the_day_are_asked_for_in_words_and_never_as_a_date(self) -> None:
        """The platform has no clock: it cannot know what day tomorrow is, so a model is not asked
        to write a date, and nothing in the skill makes one."""
        manifests = platform_skills(FakeSpeech()).manifests()

        for name in ("alarm", "alarm-postpone", "alarm-change"):
            for parameter in manifests[name].parameters.values():
                assert parameter.type.value == "string", name
                assert "words" in (parameter.description or ""), name
            assert "date" not in " ".join(
                parameter.description or "" for parameter in manifests[name].parameters.values()
            ), name

    def test_they_are_found_by_what_they_say_of_themselves_in_both_languages(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()
        italian = {
            "alarm": ("metti la sveglia", "svegliami"),
            "alarm-cancel": ("cancella", "spegni la sveglia"),
            "alarm-postpone": ("rimanda la sveglia", "ancora cinque minuti"),
            "alarm-change": ("sposta la sveglia", "cambia la sveglia"),
            "alarm-status": ("che ora suona", "quali sveglie"),
        }

        for name in ROOM_FOR_ALL:
            examples = " ".join(manifests[name].examples)
            assert "alarm" in examples, name
            assert any(word in examples for word in italian[name]), name

    def test_each_tenant_is_given_all_of_them_and_nobody_elses(self) -> None:
        import asyncio

        async def check() -> None:
            store, core = InMemoryStore(), platform_skills(FakeSpeech())
            one, two = make_tenant_id(), make_tenant_id()
            await core.ensure(store, one, now=START)
            await core.ensure(store, two, now=START)

            mine = await store.list_skills(one, [Owner.of_subject(Subject.tenant(one))])
            yours = await store.list_skills(two, [Owner.of_subject(Subject.tenant(two))])

            assert set(ROOM_FOR_ALL) <= {s.name for s in mine}
            assert sorted(s.name for s in mine) == sorted(s.name for s in yours)
            assert {s.id for s in mine}.isdisjoint({s.id for s in yours})

        asyncio.run(check())


class TestSettingAnAlarm:
    @pytest.mark.parametrize(
        ("time", "day", "hour", "minute", "wanted"),
        [
            ("alle sette e mezza", None, 7, 30, AlarmDay()),
            ("seven thirty", None, 7, 30, AlarmDay()),
            ("alle 7", "domani", 7, 0, AlarmDay(in_days=1)),
            ("8", "lunedì", 8, 0, AlarmDay(weekday=Weekday.MONDAY)),
            ("seven", "the 15th of october", 7, 0, AlarmDay(day=15, month=10)),
            ("7 pm", "tomorrow", 19, 0, AlarmDay(in_days=1)),
            ("alle 7", "stasera", 19, 0, AlarmDay(in_days=0)),
            ("sette di sera", None, 19, 0, AlarmDay()),
            ("mezzanotte", "il 31 dicembre", 0, 0, AlarmDay(day=31, month=12)),
        ],
    )
    async def test_the_hour_and_the_day_are_given_to_the_device_as_the_person_said_them(
        self, time: str, day: str | None, hour: int, minute: int, wanted: AlarmDay
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        params: dict[str, ParamValue] = {"time": time, **({} if day is None else {"day": day})}

        done = await Alarm(speech).run(
            request_of(tenant, "alarm", **params), a_device(tenant), language="en"
        )

        [command] = done.commands
        assert isinstance(command, SetAlarm)
        assert (command.hour, command.minute, command.day) == (hour, minute, wanted)
        assert done.message is None  # what is said is what the device answers

    async def test_no_date_is_ever_worked_out_here(self) -> None:
        """The device is told *tomorrow*, and the platform does not know what day that is."""
        tenant = make_tenant_id()

        done = await Alarm(FakeSpeech()).run(
            request_of(tenant, "alarm", time="7", day="tomorrow"), a_device(tenant), language="en"
        )

        [command] = done.commands
        assert isinstance(command, SetAlarm)
        assert command.day == AlarmDay(in_days=1)
        assert set(command.day.model_dump(exclude_none=True)) == {"in_days"}
        assert done.audit == "alarm set for 07:00 (in_days 1)"

    async def test_the_trail_says_it_is_for_the_next_time_when_no_day_was_said(self) -> None:
        tenant = make_tenant_id()

        done = await Alarm(FakeSpeech()).run(
            request_of(tenant, "alarm", time="7:30"), a_device(tenant), language="en"
        )

        assert done.audit == "alarm set for 07:30 (the next time)"

    @pytest.mark.parametrize(
        ("language", "words"), [("en", "Wake up! It is time."), ("it", "Sveglia! È ora.")]
    )
    async def test_what_rings_is_a_sentence_made_a_voice_once_when_it_is_set(
        self, language: str, words: str
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        done = await Alarm(speech).run(
            request_of(tenant, "alarm", time="7"), a_device(tenant), language=language
        )

        [command] = done.commands
        assert isinstance(command, SetAlarm)
        assert command.text == words
        assert FakeSpeech.words_in(command.audio) == words
        assert speech.asked == [(Owner.of_subject(Subject.tenant(tenant)), words)]

    async def test_it_is_made_with_the_voice_of_whom_the_device_acts_for(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        mine = Principal(
            tenant_id=tenant,
            device_id=make_device_id(),
            acts_as=Subject.user(UserId(uuid4())),
            roles=frozenset({Role.MEMBER}),
        )

        await Alarm(speech).run(request_of(tenant, "alarm", time="7"), mine, language="en")

        assert speech.asked[0][0] == Owner.of_subject(mine.acts_as)

    @pytest.mark.parametrize(
        "params",
        [
            {"time": "soon"},
            {"time": "25"},
            {"time": ""},
            {"time": 7},
            {"time": "7", "day": "every day"},
            {"time": "7", "day": "domani lunedì"},
            {"time": "in ten minutes"},
            {"time": "tra dieci minuti"},
            {"day": "tomorrow"},
            {"time": "7", "day": 3},
            {},
        ],
    )
    async def test_what_is_not_an_hour_and_a_day_is_a_skill_that_says_so_and_nothing_is_set(
        self, params: dict[str, ParamValue]
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        with pytest.raises(SkillFailedError) as raised:
            await Alarm(speech).run(
                request_of(tenant, "alarm", **params), a_device(tenant), language="en"
            )

        assert raised.value.message == "I did not understand what time that is."
        assert speech.asked == []  # no voice is made for what will not be set

    async def test_and_it_says_so_in_italian_to_a_person_who_spoke_it(self) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError) as raised:
            await Alarm(FakeSpeech()).run(
                request_of(tenant, "alarm", time="tra poco"), a_device(tenant), language="it"
            )

        assert raised.value.message == "Non ho capito che ora intendi."

    async def test_a_voice_that_cannot_be_made_is_a_skill_that_failed_and_says_what_kind(
        self,
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        speech.failures = [GatewayUnavailableError("http://10.0.0.7:8080 refused, key sk-abc")]

        with pytest.raises(SkillRunError) as raised:
            await Alarm(speech).run(
                request_of(tenant, "alarm", time="7"), a_device(tenant), language="en"
            )

        assert raised.value.message == "the voice could not be made (gateway_unavailable)"
        assert "sk-abc" not in raised.value.message

    @pytest.mark.parametrize(
        ("at", "language", "said"),
        [
            (TOMORROW_AT_SEVEN, "en", "Alarm set for tomorrow at 7 AM."),
            (TOMORROW_AT_SEVEN, "it", "Sveglia impostata per domani alle 7."),
            (civil(2026, 10, 13, 23, 30), "en", "Alarm set for today at 11:30 PM."),
            (civil(2026, 10, 19, 7, 30), "it", "Sveglia impostata per lunedì alle 7 e 30."),
            (civil(2026, 11, 3, 6, 0), "en", "Alarm set for November 3 at 6 AM."),
        ],
    )
    async def test_it_says_the_moment_that_the_device_chose_from_where_the_device_is(
        self, at: datetime, language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await Alarm(FakeSpeech()).run(
            request_of(tenant, "alarm", time="7"), a_device(tenant), language=language
        )

        assert done.after is not None
        assert done.after(set_to(at)) == said

    @pytest.mark.parametrize(
        ("reason", "language", "said"),
        [
            ("past", "en", "That time has already gone by."),
            ("past", "it", "Quell'ora è già passata."),
            ("no_such_day", "en", "There is no such day."),
            ("no_such_day", "it", "Quel giorno non esiste."),
            ("too_many", "en", "There are too many alarms already."),
            ("too_many", "it", "Ci sono già troppe sveglie."),
        ],
    )
    async def test_a_moment_that_cannot_be_is_said_by_the_skill_in_its_words(
        self, reason: str, language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await Alarm(FakeSpeech()).run(
            request_of(tenant, "alarm", time="7", day="today"),
            a_device(tenant),
            language=language,
        )

        assert done.after is not None
        with pytest.raises(SkillFailedError) as raised:
            done.after(answered({"now": NOW.isoformat(), "reason": reason}))
        assert raised.value.message == said

    @pytest.mark.parametrize("answer", [None, {}, {"alarm": "7"}, {"now": "tomorrow"}])
    async def test_a_device_that_does_not_say_which_alarm_it_set_is_a_skill_that_failed(
        self, answer: Mapping[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await Alarm(FakeSpeech()).run(
            request_of(tenant, "alarm", time="7"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say which alarm it set"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestCancellingAnAlarm:
    async def test_with_none_named_the_device_chooses_and_is_told_none(self) -> None:
        tenant = make_tenant_id()

        done = await AlarmCancel().run(
            request_of(tenant, "alarm-cancel"), a_device(tenant), language="en"
        )

        assert done.commands == (CancelAlarm(),)
        assert done.message is None
        assert done.audit == "asked to cancel the next alarm to ring"

    async def test_the_one_that_was_named_is_named_by_its_hour_and_its_minute(self) -> None:
        tenant = make_tenant_id()

        done = await AlarmCancel().run(
            request_of(tenant, "alarm-cancel", time="alle sette e mezza"),
            a_device(tenant),
            language="it",
        )

        assert done.commands == (CancelAlarm(hour=7, minute=30),)
        assert done.audit == "asked to cancel the alarm at 7:30"

    @pytest.mark.parametrize("words", ["soon", "", "25", "tra poco"])
    async def test_an_hour_that_is_not_one_is_said_and_nothing_is_cancelled(
        self, words: str
    ) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError):
            await AlarmCancel().run(
                request_of(tenant, "alarm-cancel", time=words), a_device(tenant), language="en"
            )

    @pytest.mark.parametrize(
        ("cancelled", "named", "language", "said"),
        [
            (
                (alarm(TOMORROW_AT_SEVEN),),
                None,
                "en",
                "The alarm for tomorrow at 7 AM is cancelled.",
            ),
            ((alarm(TOMORROW_AT_SEVEN),), None, "it", "Sveglia per domani alle 7 annullata."),
            (
                (alarm(TOMORROW_AT_SEVEN), alarm(civil(2026, 10, 14, 8))),
                None,
                "en",
                "2 alarms are cancelled.",
            ),
            (
                (alarm(TOMORROW_AT_SEVEN), alarm(civil(2026, 10, 14, 8))),
                None,
                "it",
                "Ho annullato 2 sveglie.",
            ),
            ((), None, "en", "There is no alarm to cancel."),
            ((), None, "it", "Non c'è nessuna sveglia da annullare."),
            ((), "seven thirty", "en", "There is no alarm at 7:30 AM."),
            ((), "alle sette e mezza", "it", "Non c'è nessuna sveglia alle 7 e 30."),
        ],
    )
    async def test_it_says_what_the_device_stopped(
        self, cancelled: tuple[AlarmInfo, ...], named: str | None, language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        params: dict[str, ParamValue] = {} if named is None else {"time": named}
        done = await AlarmCancel().run(
            request_of(tenant, "alarm-cancel", **params), a_device(tenant), language=language
        )

        assert done.after is not None
        answer = AlarmsCancelled(now=NOW, cancelled=cancelled, remaining=0)
        assert done.after(answered(data_of(answer))) == said

    @pytest.mark.parametrize("answer", [None, {}, {"remaining": 0}, {"now": "x", "remaining": 0}])
    async def test_a_device_that_does_not_say_what_it_stopped_is_a_skill_that_failed(
        self, answer: Mapping[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await AlarmCancel().run(
            request_of(tenant, "alarm-cancel"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say which alarm it cancelled"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestPuttingAnAlarmOff:
    async def test_with_no_length_said_how_long_is_the_device_s_to_choose(self) -> None:
        tenant = make_tenant_id()

        done = await AlarmPostpone().run(
            request_of(tenant, "alarm-postpone"), a_device(tenant), language="en"
        )

        assert done.commands == (PostponeAlarm(),)  # the skill has no snooze to tell
        assert done.message is None
        assert done.audit == "asked to put the alarm off by the snooze of the device"

    @pytest.mark.parametrize(
        ("words", "seconds"),
        [
            ("ten minutes", 600),
            ("dieci minuti", 600),
            ("half an hour", 1800),
            ("mezz'ora", 1800),
            ("5 minutes", 300),
            ("un quarto d'ora", 900),
            ("un'ora e mezza", 5400),
            ("1 second", 1),
            ("24 hours", 86_400),
        ],
    )
    async def test_the_length_a_person_said_is_read_by_code_and_passed_on(
        self, words: str, seconds: int
    ) -> None:
        tenant = make_tenant_id()

        done = await AlarmPostpone().run(
            request_of(tenant, "alarm-postpone", by=words), a_device(tenant), language="en"
        )

        assert done.commands == (PostponeAlarm(seconds=seconds),)
        assert done.audit == f"asked to put the alarm off by {seconds} s"

    @pytest.mark.parametrize(
        "words", ["soon", "", "a while", "tra poco", "mezzogiorno", "two days"]
    )
    async def test_a_length_that_is_not_one_is_said_and_nothing_is_put_off(
        self, words: str
    ) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError) as raised:
            await AlarmPostpone().run(
                request_of(tenant, "alarm-postpone", by=words), a_device(tenant), language="en"
            )

        assert raised.value.message == "I did not understand how long that is."

    @pytest.mark.parametrize("words", ["0 minutes", "48 hours", "1500 minutes", "86401 seconds"])
    async def test_a_length_out_of_the_range_of_a_day_says_so(self, words: str) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError) as raised:
            await AlarmPostpone().run(
                request_of(tenant, "alarm-postpone", by=words), a_device(tenant), language="it"
            )

        assert raised.value.message == "Una sveglia si può rimandare da un secondo a un giorno."

    async def test_a_length_that_is_not_a_text_is_not_a_length(self) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError, match="how long"):
            await AlarmPostpone().run(
                request_of(tenant, "alarm-postpone", by=10), a_device(tenant), language="en"
            )

    @pytest.mark.parametrize(
        ("postponed", "language", "said"),
        [
            (
                (alarm(civil(2026, 10, 13, 22, 15)),),
                "en",
                "The alarm will ring today at 10:15 PM.",
            ),
            (
                (alarm(civil(2026, 10, 13, 22, 15)),),
                "it",
                "La sveglia suonerà oggi alle 22 e 15.",
            ),
            (
                (alarm(civil(2026, 10, 14, 7, 10)),),
                "en",
                "The alarm will ring tomorrow at 7:10 AM.",
            ),
            (
                (alarm(civil(2026, 10, 14, 7, 10)),),
                "it",
                "La sveglia suonerà domani alle 7 e 10.",
            ),
            (
                (alarm(civil(2026, 10, 14, 7, 10)), alarm(civil(2026, 10, 14, 8, 10))),
                "en",
                "2 alarms are put off.",
            ),
            (
                (alarm(civil(2026, 10, 14, 7, 10)), alarm(civil(2026, 10, 14, 8, 10))),
                "it",
                "Ho rimandato 2 sveglie.",
            ),
        ],
    )
    async def test_it_says_where_the_alarm_is_now_as_the_device_has_it(
        self, postponed: tuple[AlarmInfo, ...], language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await AlarmPostpone().run(
            request_of(tenant, "alarm-postpone"), a_device(tenant), language=language
        )

        assert done.after is not None
        answer = AlarmsPostponed(now=NOW, postponed=postponed)
        assert done.after(answered(data_of(answer))) == said

    @pytest.mark.parametrize(
        ("language", "said"),
        [("en", "There is no alarm to change."), ("it", "Non c'è nessuna sveglia da cambiare.")],
    )
    async def test_with_no_alarm_to_put_off_the_person_is_told_so(
        self, language: str, said: str
    ) -> None:
        """As for a cancel: there being none is a thing to say, and no skill has failed."""
        tenant = make_tenant_id()
        done = await AlarmPostpone().run(
            request_of(tenant, "alarm-postpone"), a_device(tenant), language=language
        )

        assert done.after is not None
        assert done.after(answered(data_of(AlarmsPostponed(now=NOW)))) == said

    @pytest.mark.parametrize("answer", [None, {}, {"postponed": []}, {"now": "tomorrow"}])
    async def test_a_device_that_does_not_say_which_alarm_it_put_off_is_a_skill_that_failed(
        self, answer: Mapping[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await AlarmPostpone().run(
            request_of(tenant, "alarm-postpone"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say which alarm it put off"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestChangingTheHourOfAnAlarm:
    @pytest.mark.parametrize(
        ("time", "day", "was", "wanted", "named"),
        [
            ("alle otto", None, None, (8, 0, AlarmDay()), None),
            ("seven thirty", None, None, (7, 30, AlarmDay()), None),
            ("alle 7", "domani", None, (7, 0, AlarmDay(in_days=1)), None),
            ("alle otto", None, "alle sette", (8, 0, AlarmDay()), (7, 0)),
            ("8", "lunedì", "7:30", (8, 0, AlarmDay(weekday=Weekday.MONDAY)), (7, 30)),
            ("seven", "the 15th of october", None, (7, 0, AlarmDay(day=15, month=10)), None),
        ],
    )
    async def test_the_new_hour_the_day_and_the_alarm_are_given_as_the_person_said_them(
        self,
        time: str,
        day: str | None,
        was: str | None,
        wanted: tuple[int, int, AlarmDay],
        named: tuple[int, int] | None,
    ) -> None:
        tenant = make_tenant_id()
        params: dict[str, ParamValue] = {"time": time}
        if day is not None:
            params["day"] = day
        if was is not None:
            params["from"] = was

        done = await AlarmChange().run(
            request_of(tenant, "alarm-change", **params), a_device(tenant), language="en"
        )

        hour, minute, when = wanted
        from_hour, from_minute = named or (None, None)
        assert done.commands == (
            MoveAlarm(
                hour=hour, minute=minute, day=when, from_hour=from_hour, from_minute=from_minute
            ),
        )
        assert done.message is None

    async def test_no_date_is_ever_worked_out_here_either(self) -> None:
        tenant = make_tenant_id()

        done = await AlarmChange().run(
            request_of(tenant, "alarm-change", time="7", day="tomorrow"),
            a_device(tenant),
            language="en",
        )

        [command] = done.commands
        assert isinstance(command, MoveAlarm)
        assert set(command.day.model_dump(exclude_none=True)) == {"in_days"}
        assert done.audit == "asked to move the next alarm to ring to 07:00 (in_days 1)"

    async def test_the_trail_says_which_alarm_was_named(self) -> None:
        tenant = make_tenant_id()

        done = await AlarmChange().run(
            request_of(tenant, "alarm-change", time="8", **{"from": "alle sette e mezza"}),
            a_device(tenant),
            language="it",
        )

        assert done.audit == "asked to move the alarm at 7:30 to 08:00 (the next time)"

    @pytest.mark.parametrize(
        "params",
        [
            {"time": "soon"},
            {"time": "25"},
            {"time": ""},
            {"time": 8},
            {"time": "8", "day": "every day"},
            {"time": "8", "day": 3},
            {"time": "in ten minutes"},
            {"time": "8", "from": "soon"},
            {"time": "8", "from": ""},
            {"day": "tomorrow"},
            {},
        ],
    )
    async def test_what_is_not_an_hour_and_a_day_is_a_skill_that_says_so_and_nothing_is_moved(
        self, params: dict[str, ParamValue]
    ) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError) as raised:
            await AlarmChange().run(
                request_of(tenant, "alarm-change", **params), a_device(tenant), language="en"
            )

        assert raised.value.message == "I did not understand what time that is."

    @pytest.mark.parametrize(
        ("moved", "language", "said"),
        [
            ((alarm(TOMORROW_AT_SEVEN),), "en", "The alarm will ring tomorrow at 7 AM."),
            ((alarm(TOMORROW_AT_SEVEN),), "it", "La sveglia suonerà domani alle 7."),
            (
                (alarm(civil(2026, 10, 19, 8, 30)),),
                "it",
                "La sveglia suonerà lunedì alle 8 e 30.",
            ),
            (
                (alarm(TOMORROW_AT_SEVEN), alarm(civil(2026, 10, 14, 8))),
                "en",
                "2 alarms are moved.",
            ),
            (
                (alarm(TOMORROW_AT_SEVEN), alarm(civil(2026, 10, 14, 8))),
                "it",
                "Ho spostato 2 sveglie.",
            ),
        ],
    )
    async def test_it_says_where_the_alarm_is_now_as_the_device_has_it(
        self, moved: tuple[AlarmInfo, ...], language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await AlarmChange().run(
            request_of(tenant, "alarm-change", time="7"), a_device(tenant), language=language
        )

        assert done.after is not None
        assert done.after(answered(data_of(AlarmsMoved(now=NOW, moved=moved)))) == said

    @pytest.mark.parametrize(
        ("reason", "language", "said"),
        [
            ("past", "en", "That time has already gone by."),
            ("past", "it", "Quell'ora è già passata."),
            ("no_such_day", "en", "There is no such day."),
            ("no_such_day", "it", "Quel giorno non esiste."),
        ],
    )
    async def test_a_moment_that_cannot_be_is_a_skill_that_failed_and_says_why_in_its_words(
        self, reason: str, language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await AlarmChange().run(
            request_of(tenant, "alarm-change", time="8", day="oggi"),
            a_device(tenant),
            language=language,
        )

        assert done.after is not None
        with pytest.raises(SkillFailedError) as raised:
            done.after(answered({"now": NOW.isoformat(), "reason": reason}))
        assert raised.value.message == said

    @pytest.mark.parametrize(
        ("named", "language", "said"),
        [
            (None, "en", "There is no alarm to change."),
            (None, "it", "Non c'è nessuna sveglia da cambiare."),
            ("seven thirty", "en", "There is no alarm at 7:30 AM."),
            ("alle sette e mezza", "it", "Non c'è nessuna sveglia alle 7 e 30."),
        ],
    )
    async def test_with_no_alarm_to_move_the_person_is_told_so(
        self, named: str | None, language: str, said: str
    ) -> None:
        """As for a cancel: there being none, or none at that hour, is a thing to say."""
        tenant = make_tenant_id()
        params: dict[str, ParamValue] = {"time": "8"}
        if named is not None:
            params["from"] = named
        done = await AlarmChange().run(
            request_of(tenant, "alarm-change", **params), a_device(tenant), language=language
        )

        assert done.after is not None
        assert done.after(answered({"now": NOW.isoformat(), "reason": "none"})) == said

    @pytest.mark.parametrize("answer", [None, {}, {"moved": []}, {"now": NOW.isoformat()}])
    async def test_a_device_that_does_not_say_which_alarm_it_moved_is_a_skill_that_failed(
        self, answer: Mapping[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await AlarmChange().run(
            request_of(tenant, "alarm-change", time="8"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say which alarm it moved"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestSayingWhatIsSet:
    async def test_the_device_is_asked_and_nothing_is_changed(self) -> None:
        tenant = make_tenant_id()

        done = await AlarmStatus().run(
            request_of(tenant, "alarm-status"), a_device(tenant), language="en"
        )

        assert done.commands == (GetState(),)
        assert done.message is None
        assert done.audit == "asked what alarms the device has"

    @pytest.mark.parametrize(
        ("alarms", "language", "said"),
        [
            ((), "en", "There are no alarms."),
            ((), "it", "Non ci sono sveglie."),
            ((alarm(TOMORROW_AT_SEVEN),), "en", "Your alarm is set for tomorrow at 7 AM."),
            ((alarm(TOMORROW_AT_SEVEN),), "it", "La sveglia è per domani alle 7."),
            ((alarm(civil(2026, 10, 13, 23), ringing=True),), "en", "The alarm is ringing."),
            ((alarm(civil(2026, 10, 13, 23), ringing=True),), "it", "La sveglia sta suonando."),
            (
                (alarm(TOMORROW_AT_SEVEN), alarm(civil(2026, 10, 19, 8, 30))),
                "en",
                "You have 2 alarms: tomorrow at 7 AM, Monday at 8:30 AM.",
            ),
            (
                (alarm(TOMORROW_AT_SEVEN), alarm(civil(2026, 10, 19, 8, 30))),
                "it",
                "Hai 2 sveglie: domani alle 7, lunedì alle 8 e 30.",
            ),
            (
                (alarm(civil(2026, 10, 13, 23), ringing=True), alarm(TOMORROW_AT_SEVEN)),
                "it",
                "Hai 2 sveglie: una sta suonando, domani alle 7.",
            ),
        ],
    )
    async def test_what_the_device_holds_is_said_in_the_language_of_the_person(
        self, alarms: tuple[AlarmInfo, ...], language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await AlarmStatus().run(
            request_of(tenant, "alarm-status"), a_device(tenant), language=language
        )

        assert done.after is not None
        assert done.after(holding(*alarms)) == said

    @pytest.mark.parametrize(
        "answer",
        [
            None,
            {},
            {"volume": 40, "muted": False},
            {"now": "tomorrow"},
            {"now": NOW.isoformat(), "alarms": 3},
        ],
    )
    async def test_a_device_that_does_not_say_what_it_holds_is_a_skill_that_failed(
        self, answer: Mapping[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await AlarmStatus().run(
            request_of(tenant, "alarm-status"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say what alarms"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestFromAMessageToADevice:
    """The whole path in the service, with the answers of a device made up."""

    @staticmethod
    def rig(speech: FakeSpeech | None = None) -> ActionsRig:
        return ActionsRig(home_text=HOME_TEXT, core=platform_skills(speech or FakeSpeech()))

    async def test_an_alarm_reaches_the_speaker_that_was_spoken_to_with_the_day_as_it_was_said(
        self,
    ) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(
            data_of(AlarmSet(now=NOW, alarm=alarm(TOMORROW_AT_SEVEN)))
        )
        rig.plans(plan_of("alarm", parameters={"time": "7", "day": "tomorrow"}))

        outcome = await rig.hear(KITCHEN, "set an alarm for 7 tomorrow")

        assert (outcome.status, outcome.reply, outcome.skill) == (
            ActStatus.DONE,
            "Alarm set for tomorrow at 7 AM.",
            "alarm",
        )
        [(tenant, device, command)] = rig.dispatcher.given
        assert (tenant, device) == (rig.tenant, rig.device(KITCHEN).id)
        assert isinstance(command, SetAlarm)
        assert (command.hour, command.minute, command.day) == (7, 0, AlarmDay(in_days=1))
        assert rig.runner.requests == []  # a skill of the platform: no sandbox
        [record] = await rig.audit()
        assert (record.skill_name, record.result, record.detail) == (
            "alarm",
            ActionResult.EXECUTED,
            "alarm set for 07:00 (in_days 1)",
        )

    async def test_the_words_of_the_hour_must_be_in_the_message_or_the_person_is_asked(
        self,
    ) -> None:
        rig = self.rig()
        rig.plans(plan_of("alarm", parameters={"time": "8"}))

        outcome = await rig.hear(KITCHEN, "set an alarm for 7")

        assert outcome.status is ActStatus.NEEDS_MORE
        assert rig.dispatcher.given == []

    async def test_a_moment_that_has_gone_by_is_said_by_the_skill_in_its_words(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(data_of(AlarmSet(now=NOW, reason="past")))
        rig.plans(plan_of("alarm", parameters={"time": "7", "day": "oggi"}, language="it"))

        outcome = await rig.hear(KITCHEN, "metti la sveglia per oggi alle 7")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
        assert outcome.reply == core_say("it", CoreSay.ALARM_PAST)
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED

    async def test_a_cancel_goes_the_same_way(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(
            data_of(AlarmsCancelled(now=NOW, cancelled=(alarm(TOMORROW_AT_SEVEN),), remaining=0))
        )
        rig.plans(plan_of("alarm-cancel"))

        outcome = await rig.hear(KITCHEN, "cancel the alarm")

        assert (outcome.status, outcome.reply) == (
            ActStatus.DONE,
            "The alarm for tomorrow at 7 AM is cancelled.",
        )
        assert rig.dispatcher.given == [(rig.tenant, rig.device(KITCHEN).id, CancelAlarm())]

    async def test_a_snooze_reaches_the_speaker_with_the_length_that_was_said(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(
            data_of(AlarmsPostponed(now=NOW, postponed=(alarm(civil(2026, 10, 13, 22, 11)),)))
        )
        rig.plans(plan_of("alarm-postpone", parameters={"by": "ten minutes"}))

        outcome = await rig.hear(KITCHEN, "put the alarm off by ten minutes")

        assert (outcome.status, outcome.reply, outcome.skill) == (
            ActStatus.DONE,
            "The alarm will ring today at 10:11 PM.",
            "alarm-postpone",
        )
        assert rig.dispatcher.given == [
            (rig.tenant, rig.device(KITCHEN).id, PostponeAlarm(seconds=600))
        ]
        assert rig.runner.requests == []  # a skill of the platform: no sandbox
        [record] = await rig.audit()
        assert (record.skill_name, record.result, record.detail) == (
            "alarm-postpone",
            ActionResult.EXECUTED,
            "asked to put the alarm off by 600 s",
        )

    async def test_a_length_that_the_message_does_not_have_is_left_to_the_device(self) -> None:
        """ADR 0028: a value is one that the person said. *Snooze* says no length, and a model that
        wrote *twenty minutes* made one up: the device is told none and puts the alarm off by the
        snooze that it has."""
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(
            data_of(AlarmsPostponed(now=NOW, postponed=(alarm(civil(2026, 10, 13, 22, 11)),)))
        )
        rig.plans(plan_of("alarm-postpone", parameters={"by": "twenty minutes"}))

        outcome = await rig.hear(KITCHEN, "snooze the alarm")

        assert outcome.status is ActStatus.DONE
        assert rig.dispatcher.given == [(rig.tenant, rig.device(KITCHEN).id, PostponeAlarm())]

    async def test_a_move_goes_the_same_way_with_the_day_as_it_was_said(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(
            data_of(AlarmsMoved(now=NOW, moved=(alarm(civil(2026, 10, 14, 8, 0)),)))
        )
        rig.plans(plan_of("alarm-change", parameters={"time": "8", "day": "tomorrow"}))

        outcome = await rig.hear(KITCHEN, "move the alarm to 8 tomorrow")

        assert (outcome.status, outcome.reply) == (
            ActStatus.DONE,
            "The alarm will ring tomorrow at 8 AM.",
        )
        assert rig.dispatcher.given == [
            (
                rig.tenant,
                rig.device(KITCHEN).id,
                MoveAlarm(hour=8, minute=0, day=AlarmDay(in_days=1)),
            )
        ]

    async def test_an_hour_that_has_gone_by_is_said_by_the_skill_in_its_words(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(data_of(AlarmsMoved(now=NOW, reason="past")))
        rig.plans(plan_of("alarm-change", parameters={"time": "7", "day": "oggi"}, language="it"))

        outcome = await rig.hear(KITCHEN, "sposta la sveglia a oggi alle 7")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
        assert outcome.reply == core_say("it", CoreSay.ALARM_PAST)
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED

    async def test_the_new_hour_must_be_in_the_message_or_the_person_is_asked(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("alarm-change", parameters={"time": "9"}))

        outcome = await rig.hear(KITCHEN, "move the alarm to 8")

        assert outcome.status is ActStatus.NEEDS_MORE
        assert rig.dispatcher.given == []
