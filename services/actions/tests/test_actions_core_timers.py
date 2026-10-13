"""The skills of the platform for the timers of a device (ADR 0029): one is set, cancelled, and
asked how much time is left on. Each is the interface between a model and the clock of a device: it
passes on the seconds that code makes of the words of the person and the sound that rings, and
says what the device answered in a sentence of the code, in the language of the person."""

from collections.abc import Mapping, Sequence
from uuid import uuid4

import pytest

from factotum.actions.core import (
    Timer,
    TimerCancel,
    TimerStatus,
    platform_skills,
)
from factotum.actions.core.phrases import CoreSay, core_say
from factotum.actions.rig import HOME_TEXT, ActionsRig, plan_of
from factotum.actions.store import InMemoryStore
from factotum.actions.testing import START, FakeSpeech
from factotum.contracts.actions import ActCode, ActStatus
from factotum.contracts.audit import ActionResult
from factotum.contracts.commands import (
    MAX_TIMER_SECONDS,
    CancelTimer,
    CommandResult,
    GetState,
    SetTimer,
)
from factotum.contracts.effects import Effect
from factotum.contracts.errors import GatewayUnavailableError, SkillFailedError, SkillRunError
from factotum.contracts.identity import Owner, Principal, Role, Subject
from factotum.contracts.ids import SkillId, TenantId, UserId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import ParamValue, SkillMode, SkillTarget
from factotum.contracts.testing import make_device_id, make_tenant_id
from factotum.contracts.timekeeping import (
    TimerInfo,
    TimersCancelled,
    TimerSet,
    TimeState,
    data_of,
)

KITCHEN = "Kitchen speaker"
NOW = "2026-10-13T22:00:00"
ROOM_FOR_ALL = ("timer", "timer-cancel", "timer-status")


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


def timer(seconds: int, left: int | None = None, *, ringing: bool = False) -> TimerInfo:
    return TimerInfo(
        id=f"t{seconds}", seconds=seconds, left_s=0 if ringing else left or seconds, ringing=ringing
    )


def answered(data: Mapping[str, object] | None) -> Sequence[CommandResult]:
    return [CommandResult.ok(data)]  # type: ignore[arg-type]


def holding(*timers: TimerInfo) -> Sequence[CommandResult]:
    return answered({"volume": 40, **data_of(TimeState(now=NOW, timers=timers))})  # type: ignore[arg-type]


class TestWhatTheyAre:
    def test_they_are_skills_of_the_platform_that_act_on_the_device_that_was_spoken_to(
        self,
    ) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        for name in ROOM_FOR_ALL:
            assert manifests[name].target is SkillTarget.SYSTEM, name
            assert manifests[name].entrypoint is None, name  # no script: they are code
            assert manifests[name].permissions.network == (), name  # and reach nothing
            assert manifests[name].permissions.secrets == (), name

    def test_what_they_change_can_be_undone_and_asking_changes_nothing(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        assert manifests["timer"].effect is Effect.REVERSIBLE
        assert manifests["timer-cancel"].effect is Effect.REVERSIBLE
        assert manifests["timer-status"].effect is Effect.READ_ONLY

    def test_each_asks_for_what_it_cannot_do_without_and_nothing_else(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        assert manifests["timer"].required == ("duration",)
        assert set(manifests["timer"].parameters) == {"duration"}
        assert manifests["timer-cancel"].required == ()  # which one is the device's to choose
        assert set(manifests["timer-cancel"].parameters) == {"duration"}
        assert manifests["timer-status"].parameters == {}

    def test_the_length_is_asked_for_in_words_and_never_as_a_number_of_seconds(self) -> None:
        """A model that is asked for the seconds writes 300 for five minutes, and 300 is a number
        that was not said (ADR 0028): so it is asked for what was said."""
        manifests = platform_skills(FakeSpeech()).manifests()

        parameter = manifests["timer"].parameters["duration"]
        assert parameter.type.value == "string"
        assert "words" in (parameter.description or "")

    def test_they_are_found_by_what_they_say_of_themselves_in_both_languages(self) -> None:
        manifests = platform_skills(FakeSpeech()).manifests()

        italian = {
            "timer": ("metti", "imposta", "avvisami"),
            "timer-cancel": ("annulla", "ferma", "spegni"),
            "timer-status": ("quanto", "ho dei"),
        }
        for name in ROOM_FOR_ALL:
            examples = " ".join(manifests[name].examples)
            assert "timer" in examples, name
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


class TestSettingATimer:
    @pytest.mark.parametrize(
        ("words", "seconds"),
        [
            ("five minutes", 300),
            ("an hour and a half", 5400),
            ("cinque minuti", 300),
            ("un'ora e mezza", 5400),
            ("mezz'ora", 1800),
            ("2h30", 9000),
        ],
    )
    async def test_the_seconds_are_made_of_the_words_by_code_and_given_to_the_device(
        self, words: str, seconds: int
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        done = await Timer(speech).run(
            request_of(tenant, "timer", duration=words), a_device(tenant), language="en"
        )

        [command] = done.commands
        assert isinstance(command, SetTimer)
        assert command.seconds == seconds
        assert done.audit == f"timer set for {seconds} s"
        assert done.message is None  # what is said is what the device answers

    async def test_what_rings_is_a_sentence_made_a_voice_once_when_it_is_set(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        done = await Timer(speech).run(
            request_of(tenant, "timer", duration="five minutes"), a_device(tenant), language="en"
        )

        [command] = done.commands
        assert isinstance(command, SetTimer)
        assert command.text == "Your timer for 5 minutes is done."
        assert FakeSpeech.words_in(command.audio) == command.text
        assert speech.asked == [(Owner.of_subject(Subject.tenant(tenant)), command.text)]

    async def test_and_in_the_language_of_the_person(self) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        done = await Timer(speech).run(
            request_of(tenant, "timer", duration="cinque minuti"), a_device(tenant), language="it"
        )

        [command] = done.commands
        assert isinstance(command, SetTimer)
        assert command.text == "Il timer da 5 minuti è scaduto."

    async def test_it_is_made_with_the_voice_of_whom_the_device_acts_for(self) -> None:
        tenant = make_tenant_id()
        speech = FakeSpeech()
        mine = Principal(
            tenant_id=tenant,
            device_id=make_device_id(),
            acts_as=Subject.user(UserId(uuid4())),
            roles=frozenset({Role.MEMBER}),
        )

        await Timer(speech).run(
            request_of(tenant, "timer", duration="a minute"), mine, language="en"
        )

        assert speech.asked[0][0] == Owner.of_subject(mine.acts_as)

    @pytest.mark.parametrize("words", ["soon", "", "ten", "qualche minuto", "five", 5, True])
    async def test_words_that_are_not_a_length_are_a_skill_that_says_so_and_nothing_is_set(
        self, words: ParamValue
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        with pytest.raises(SkillFailedError) as raised:
            await Timer(speech).run(
                request_of(tenant, "timer", duration=words), a_device(tenant), language="en"
            )

        assert raised.value.message == "I did not understand how long that is."
        assert speech.asked == []  # no voice is made for what will not be set

    async def test_and_it_says_so_in_italian_to_a_person_who_spoke_it(self) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError) as raised:
            await Timer(FakeSpeech()).run(
                request_of(tenant, "timer", duration="un po'"), a_device(tenant), language="it"
            )

        assert raised.value.message == "Non ho capito di quanto tempo parli."

    async def test_a_parameter_that_is_missing_is_not_a_length_either(self) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError):
            await Timer(FakeSpeech()).run(
                request_of(tenant, "timer"), a_device(tenant), language="en"
            )

    @pytest.mark.parametrize("words", ["0 seconds", "cento ore", "25 hours", "48 ore"])
    async def test_a_timer_is_from_a_second_to_a_day(self, words: str) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()

        with pytest.raises(SkillFailedError) as raised:
            await Timer(speech).run(
                request_of(tenant, "timer", duration=words), a_device(tenant), language="en"
            )

        assert raised.value.message == "A timer can last from one second to a day."
        assert speech.asked == []

    @pytest.mark.parametrize(
        ("words", "seconds"), [("1 second", 1), ("24 hours", MAX_TIMER_SECONDS)]
    )
    async def test_and_the_ends_of_that_are_allowed(self, words: str, seconds: int) -> None:
        tenant = make_tenant_id()

        done = await Timer(FakeSpeech()).run(
            request_of(tenant, "timer", duration=words), a_device(tenant), language="en"
        )

        assert isinstance(done.commands[0], SetTimer)
        assert done.commands[0].seconds == seconds

    async def test_a_voice_that_cannot_be_made_is_a_skill_that_failed_and_says_what_kind(
        self,
    ) -> None:
        tenant, speech = make_tenant_id(), FakeSpeech()
        speech.failures = [GatewayUnavailableError("http://10.0.0.7:8080 refused, key sk-abc")]

        with pytest.raises(SkillRunError) as raised:
            await Timer(speech).run(
                request_of(tenant, "timer", duration="a minute"), a_device(tenant), language="en"
            )

        assert raised.value.message == "the voice could not be made (gateway_unavailable)"
        assert not isinstance(raised.value, SkillFailedError)  # the words are not for a person
        assert "10.0.0.7" not in raised.value.message

    @pytest.mark.parametrize(
        ("language", "said"),
        [("en", "Timer set for 5 minutes."), ("it", "Timer impostato per 5 minuti.")],
    )
    async def test_it_says_the_timer_that_the_device_set(self, language: str, said: str) -> None:
        tenant = make_tenant_id()
        done = await Timer(FakeSpeech()).run(
            request_of(tenant, "timer", duration="five minutes"),
            a_device(tenant),
            language=language,
        )

        assert done.after is not None
        assert done.after(answered(data_of(TimerSet(timer=timer(300))))) == said

    async def test_and_the_length_it_says_is_the_one_the_device_has_not_the_one_that_was_asked(
        self,
    ) -> None:
        tenant = make_tenant_id()
        done = await Timer(FakeSpeech()).run(
            request_of(tenant, "timer", duration="five minutes"), a_device(tenant), language="en"
        )

        assert done.after is not None
        assert done.after(answered(data_of(TimerSet(timer=timer(60))))) == "Timer set for 1 minute."

    async def test_a_device_that_is_full_says_so_in_the_words_of_the_skill(self) -> None:
        tenant = make_tenant_id()
        done = await Timer(FakeSpeech()).run(
            request_of(tenant, "timer", duration="five minutes"), a_device(tenant), language="it"
        )

        assert done.after is not None
        with pytest.raises(SkillFailedError) as raised:
            done.after(answered(data_of(TimerSet(reason="too_many"))))
        assert raised.value.message == "Ci sono già troppi timer."

    @pytest.mark.parametrize("answer", [None, {}, {"timer": "five"}, {"volume": 40}])
    async def test_a_device_that_does_not_say_which_timer_it_set_is_a_skill_that_failed(
        self, answer: dict[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await Timer(FakeSpeech()).run(
            request_of(tenant, "timer", duration="five minutes"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say which timer it set"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestCancellingATimer:
    async def test_with_none_named_the_device_chooses_and_is_told_none(self) -> None:
        tenant = make_tenant_id()

        done = await TimerCancel().run(
            request_of(tenant, "timer-cancel"), a_device(tenant), language="en"
        )

        assert done.commands == (CancelTimer(),)
        assert done.message is None
        assert done.audit == "asked to cancel the next timer to ring"

    async def test_the_one_that_was_named_is_named_by_its_length_in_seconds(self) -> None:
        tenant = make_tenant_id()

        done = await TimerCancel().run(
            request_of(tenant, "timer-cancel", duration="cinque minuti"),
            a_device(tenant),
            language="it",
        )

        assert done.commands == (CancelTimer(seconds=300),)
        assert done.audit == "asked to cancel the timer of 300 s"

    @pytest.mark.parametrize("words", ["soon", "", "cinque", "100 hours"])
    async def test_a_length_that_is_not_one_is_said_and_nothing_is_cancelled(
        self, words: str
    ) -> None:
        tenant = make_tenant_id()

        with pytest.raises(SkillFailedError):
            await TimerCancel().run(
                request_of(tenant, "timer-cancel", duration=words), a_device(tenant), language="en"
            )

    @pytest.mark.parametrize(
        ("cancelled", "named", "language", "said"),
        [
            ((timer(300),), None, "en", "The timer for 5 minutes is cancelled."),
            ((timer(300),), None, "it", "Timer da 5 minuti annullato."),
            ((timer(60), timer(300)), None, "en", "2 timers are cancelled."),
            ((timer(60), timer(300)), None, "it", "Ho annullato 2 timer."),
            ((), None, "en", "There is no timer to cancel."),
            ((), None, "it", "Non c'è nessun timer da annullare."),
            ((), "ten minutes", "en", "There is no timer for 10 minutes."),
            ((), "dieci minuti", "it", "Non c'è nessun timer da 10 minuti."),
        ],
    )
    async def test_it_says_what_the_device_stopped(
        self, cancelled: tuple[TimerInfo, ...], named: str | None, language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        params: dict[str, ParamValue] = {} if named is None else {"duration": named}
        done = await TimerCancel().run(
            request_of(tenant, "timer-cancel", **params), a_device(tenant), language=language
        )

        assert done.after is not None
        answer = TimersCancelled(cancelled=cancelled, remaining=0)
        assert done.after(answered(data_of(answer))) == said

    @pytest.mark.parametrize(
        "answer", [None, {}, {"cancelled": []}, {"remaining": 0, "cancelled": 1}]
    )
    async def test_a_device_that_does_not_say_what_it_stopped_is_a_skill_that_failed(
        self, answer: dict[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await TimerCancel().run(
            request_of(tenant, "timer-cancel"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say which timer it cancelled"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestSayingWhatIsLeft:
    async def test_the_device_is_asked_and_nothing_is_changed(self) -> None:
        tenant = make_tenant_id()

        done = await TimerStatus().run(
            request_of(tenant, "timer-status"), a_device(tenant), language="en"
        )

        assert done.commands == (GetState(),)
        assert done.message is None
        assert done.audit == "asked what timers the device has"

    @pytest.mark.parametrize(
        ("timers", "language", "said"),
        [
            ((), "en", "There are no timers."),
            ((), "it", "Non ci sono timer."),
            ((timer(300, 274),), "en", "The timer has 4 minutes and 34 seconds left."),
            ((timer(300, 274),), "it", "Al timer mancano 4 minuti e 34 secondi."),
            ((timer(3600, 3600),), "en", "The timer has 1 hour left."),
            ((timer(300, ringing=True),), "en", "The timer is ringing."),
            ((timer(300, ringing=True),), "it", "Il timer sta suonando."),
            (
                (timer(300, 240), timer(600, 540)),
                "en",
                "You have 2 timers: 4 minutes left, 9 minutes left.",
            ),
            (
                (timer(300, 240), timer(600, 540)),
                "it",
                "Hai 2 timer: tra 4 minuti, tra 9 minuti.",
            ),
            (
                (timer(300, ringing=True), timer(600, 540)),
                "en",
                "You have 2 timers: one is ringing, 9 minutes left.",
            ),
            (
                (timer(300, ringing=True), timer(600, 540)),
                "it",
                "Hai 2 timer: uno sta suonando, tra 9 minuti.",
            ),
        ],
    )
    async def test_what_the_device_holds_is_said_in_the_language_of_the_person(
        self, timers: tuple[TimerInfo, ...], language: str, said: str
    ) -> None:
        tenant = make_tenant_id()
        done = await TimerStatus().run(
            request_of(tenant, "timer-status"), a_device(tenant), language=language
        )

        assert done.after is not None
        assert done.after(holding(*timers)) == said

    @pytest.mark.parametrize(
        "answer",
        [None, {}, {"volume": 40, "muted": False}, {"now": "tomorrow"}, {"now": NOW, "timers": 3}],
    )
    async def test_a_device_that_does_not_say_what_it_holds_is_a_skill_that_failed(
        self, answer: dict[str, object] | None
    ) -> None:
        tenant = make_tenant_id()
        done = await TimerStatus().run(
            request_of(tenant, "timer-status"), a_device(tenant), language="en"
        )

        assert done.after is not None
        with pytest.raises(SkillRunError, match="did not say what timers"):
            done.after(answered(answer))
        with pytest.raises(SkillRunError):
            done.after([])


class TestFromAMessageToADevice:
    """The whole path in the service, with the answers of a device made up."""

    @staticmethod
    def rig(speech: FakeSpeech | None = None) -> ActionsRig:
        return ActionsRig(home_text=HOME_TEXT, core=platform_skills(speech or FakeSpeech()))

    async def test_a_timer_reaches_the_speaker_that_was_spoken_to_with_its_voice(self) -> None:
        speech, rig = FakeSpeech(), None
        rig = self.rig(speech)
        rig.dispatcher.answer = CommandResult.ok(data_of(TimerSet(timer=timer(300))))
        rig.plans(plan_of("timer", parameters={"duration": "five minutes"}))

        outcome = await rig.hear(KITCHEN, "set a timer for five minutes")

        assert (outcome.status, outcome.reply, outcome.skill) == (
            ActStatus.DONE,
            "Timer set for 5 minutes.",
            "timer",
        )
        [(tenant, device, command)] = rig.dispatcher.given
        assert (tenant, device) == (rig.tenant, rig.device(KITCHEN).id)
        assert isinstance(command, SetTimer)
        assert command.seconds == 300
        assert FakeSpeech.words_in(command.audio) == "Your timer for 5 minutes is done."
        assert rig.runner.requests == []  # a skill of the platform: no sandbox
        [record] = await rig.audit()
        assert (record.skill_name, record.result, record.detail) == (
            "timer",
            ActionResult.EXECUTED,
            "timer set for 300 s",
        )

    async def test_the_words_of_the_length_must_be_in_the_message_or_the_person_is_asked(
        self,
    ) -> None:
        rig = self.rig()
        rig.plans(plan_of("timer", parameters={"duration": "ten minutes"}))

        outcome = await rig.hear(KITCHEN, "set a timer for five minutes")

        assert outcome.status is ActStatus.NEEDS_MORE  # a length that nobody said is not one
        assert rig.dispatcher.given == []

    async def test_the_seconds_are_not_what_a_model_writes_and_a_number_it_made_up_is_refused(
        self,
    ) -> None:
        rig = self.rig()
        rig.plans(plan_of("timer", parameters={"duration": "300"}))

        outcome = await rig.hear(KITCHEN, "set a timer for five minutes")

        assert outcome.status is ActStatus.NEEDS_MORE
        assert rig.dispatcher.given == []

    async def test_a_length_that_is_not_understood_is_said_by_the_skill_in_its_words(self) -> None:
        rig = self.rig()
        rig.plans(plan_of("timer", parameters={"duration": "a while"}, language="it"))

        outcome = await rig.hear(KITCHEN, "metti un timer per un po' a while")

        assert outcome.status is ActStatus.FAILED
        assert outcome.code is ActCode.SKILL_FAILED
        assert outcome.reply == "Non ho capito di quanto tempo parli."
        assert rig.dispatcher.given == []

    async def test_a_device_that_is_full_is_a_skill_that_says_why_in_its_words(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(data_of(TimerSet(reason="too_many")))
        rig.plans(plan_of("timer", parameters={"duration": "five minutes"}, language="it"))

        outcome = await rig.hear(KITCHEN, "metti un timer di five minutes")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
        assert outcome.reply == core_say("it", CoreSay.TIMERS_FULL)
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED
        assert record.detail is not None
        assert "troppi timer" in record.detail

    async def test_a_cancel_and_a_question_go_the_same_way(self) -> None:
        rig = self.rig()
        rig.dispatcher.answer = CommandResult.ok(
            data_of(TimersCancelled(cancelled=(timer(300),), remaining=0))
        )
        rig.plans(plan_of("timer-cancel"))

        outcome = await rig.hear(KITCHEN, "cancel the timer")

        assert (outcome.status, outcome.reply) == (
            ActStatus.DONE,
            "The timer for 5 minutes is cancelled.",
        )
        assert rig.dispatcher.given == [(rig.tenant, rig.device(KITCHEN).id, CancelTimer())]
