"""How the series judges a message about a timer or an alarm (ADR 0029): the device that was spoken
to holds what was asked for, and nothing else does; what is said is the sentence of the code for
what the device holds. Windows are made by hand, as what a case can come to."""

from dataclasses import replace as changed
from datetime import datetime
from typing import Any

import pytest

from factotum.actions.plan_cases import DISPLAY, KITCHEN, PHONE, PlanCase
from factotum.contracts.actions import ActOutcome, ActStatus
from factotum.contracts.edge import TurnEnded, TurnStatus
from factotum.contracts.runner import Job, JobFile
from factotum.contracts.timekeeping import AlarmInfo, TimerInfo, TimeState
from factotum.e2e.series import PROFILES, Played, Window, judge

NAMES = tuple(PROFILES)
ENDED = TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED)


def civil(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001 - civil time


NOW = civil(2026, 10, 13, 22, 0)
"""A Tuesday evening, on the wall of every device of the series."""

TOMORROW_AT_SEVEN = civil(2026, 10, 14, 7, 0)


def timer(seconds: int, index: int = 1, *, ringing: bool = False) -> TimerInfo:
    return TimerInfo(
        id=f"t{index}", seconds=seconds, left_s=0 if ringing else seconds, ringing=ringing
    )


def alarm(at: datetime, index: int = 1, *, ringing: bool = False) -> AlarmInfo:
    return AlarmInfo(id=f"a{index}", at=at, ringing=ringing)


def kept(*things: TimerInfo | AlarmInfo) -> TimeState:
    return TimeState(
        now=NOW,
        timers=tuple(t for t in things if isinstance(t, TimerInfo)),
        alarms=tuple(a for a in things if isinstance(a, AlarmInfo)),
    )


def case(message: str, skill: str, speaker: str = KITCHEN, **params: str) -> PlanCase:
    return PlanCase(message, speaker, skill, ActStatus.DONE, None, dict(params))


def window(
    reply: str,
    skill: str,
    *,
    before: TimeState | None = None,
    after: TimeState | None = None,
    told: tuple[str, ...] = (),
    speaker: str = KITCHEN,
    **changes: Any,
) -> Window:
    """What came of a message: the device held ``before`` and holds ``after``, was ``told`` what it
    was, and the person was answered with ``reply``."""
    outcome = ActOutcome(status=ActStatus.DONE, reply=reply, skill=skill)
    held_before = dict.fromkeys(NAMES, before or kept())
    held_after = {**held_before, speaker: after if after is not None else held_before[speaker]}
    fields: dict[str, Any] = {
        "said": "words",
        "heard": "words",
        "ended": ENDED,
        "asked": 1,
        "outcome": outcome,
        "jobs": (),
        "web": (),
        "events": {**dict.fromkeys(NAMES, ()), speaker: told},
        "announced": dict.fromkeys(NAMES, ()),
        "sentences": dict.fromkeys(NAMES, 0),
        "volumes": dict.fromkeys(NAMES, 50),
        "volumes_before": dict.fromkeys(NAMES, 50),
        "muted": dict.fromkeys(NAMES, False),
        "muted_before": dict.fromkeys(NAMES, False),
        "time": held_after,
        "time_before": held_before,
        "spoken": None,
        "chat": 0,
    }
    return Window(**{**fields, **changes})


def problems(a_case: PlanCase, a_window: Window) -> list[str]:
    return judge(Played(a_case, a_window))


FIVE_MINUTES = case("set a timer for five minutes", "timer", duration="five minutes")
SEVEN_THIRTY = case("set an alarm for seven thirty", "alarm", time="seven thirty")


class TestATimerThatWasSet:
    def right(self) -> Window:
        return window(
            "Timer set for 5 minutes.",
            "timer",
            after=kept(timer(300)),
            told=("timer t1 set for 300 s",),
        )

    def test_it_is_right_when_the_device_holds_it_and_it_was_said(self) -> None:
        assert problems(FIVE_MINUTES, self.right()) == []

    def test_in_italian_it_is_said_in_italian(self) -> None:
        italian = case("metti un timer di cinque minuti", "timer", duration="cinque minuti")
        done = window(
            "Timer impostato per 5 minuti.",
            "timer",
            after=kept(timer(300)),
            told=("timer t1 set for 300 s",),
        )

        assert problems(italian, done) == []
        assert problems(italian, self.right()) != []  # English to a person who spoke Italian

    def test_a_timer_that_was_not_set_is_said(self) -> None:
        nothing = window("Timer set for 5 minutes.", "timer", told=("timer t1 set for 300 s",))

        assert problems(FIVE_MINUTES, nothing) == ["it has 0 timers, and had 0"]

    def test_two_that_were_set_are_said(self) -> None:
        twice = changed(
            self.right(), time={**self.right().time, KITCHEN: kept(timer(300), timer(300, 2))}
        )

        assert problems(FIVE_MINUTES, twice) == ["it has 2 timers, and had 0"]

    def test_a_timer_of_another_length_is_said(self) -> None:
        wrong = window(
            "Timer set for 5 minutes.",
            "timer",
            after=kept(timer(600)),
            told=("timer t1 set for 600 s",),
        )

        assert any("600" in found for found in problems(FIVE_MINUTES, wrong))

    def test_a_timer_that_has_already_run_down_is_said(self) -> None:
        running = TimerInfo(id="t1", seconds=300, left_s=100)
        wrong = window(
            "Timer set for 5 minutes.",
            "timer",
            after=kept(running),
            told=("timer t1 set for 300 s",),
        )

        assert any("100 left" in found for found in problems(FIVE_MINUTES, wrong))

    def test_a_device_that_was_told_something_else_is_said(self) -> None:
        wrong = changed(self.right(), events={**self.right().events, KITCHEN: ()})

        assert problems(FIVE_MINUTES, wrong) == ["it was told ()"]

    def test_another_sentence_is_said_with_what_it_should_have_been(self) -> None:
        wrong = changed(
            self.right(), outcome=ActOutcome(status=ActStatus.DONE, reply="Done.", skill="timer")
        )

        [found] = problems(FIVE_MINUTES, wrong)
        assert "'Done.'" in found
        assert "'Timer set for 5 minutes.'" in found

    def test_an_alarm_that_was_touched_is_said(self) -> None:
        wrong = changed(
            self.right(),
            time={**self.right().time, KITCHEN: kept(timer(300), alarm(TOMORROW_AT_SEVEN))},
        )

        assert problems(FIVE_MINUTES, wrong) == ["an alarm was touched"]

    def test_a_timer_set_on_another_device_is_said(self) -> None:
        elsewhere = window(
            "Timer set for 5 minutes.",
            "timer",
            after=kept(timer(300)),
            told=("timer t1 set for 300 s",),
            speaker=DISPLAY,
        )

        found = problems(FIVE_MINUTES, elsewhere)  # it was asked of the kitchen speaker
        assert f"{DISPLAY} had its timers or its alarms changed" in found

    def test_a_sandbox_or_a_site_that_was_used_is_said(self) -> None:
        wrong = changed(
            self.right(),
            jobs=(
                Job(
                    label="timer",
                    files=(JobFile.of("run.py", b"1"),),
                    entrypoint="run.py",
                    timeout_s=1,
                ),
            ),
        )

        assert problems(FIVE_MINUTES, wrong) == ["it ran ['timer']"]

    def test_words_that_the_speaker_did_not_say_are_said(self) -> None:
        spoken = {**dict.fromkeys(NAMES, ()), KITCHEN: ("Done.",)}
        wrong = changed(self.right(), spoken=spoken)

        found = problems(FIVE_MINUTES, wrong)
        assert any("said ('Done.',)" in line for line in found)
        right = changed(self.right(), spoken={**spoken, KITCHEN: ("Timer set for 5 minutes.",)})
        assert problems(FIVE_MINUTES, right) == []


class TestATimerThatWasCancelled:
    CANCEL = case("cancel the timer", "timer-cancel")

    def test_with_one_held_it_is_the_one_that_goes(self) -> None:
        done = window(
            "The timer for 5 minutes is cancelled.",
            "timer-cancel",
            before=kept(timer(300)),
            after=kept(),
            told=("timer t1 cancelled",),
        )

        assert problems(self.CANCEL, done) == []

    def test_with_none_held_it_is_said_that_there_is_none(self) -> None:
        done = window("There is no timer to cancel.", "timer-cancel")

        assert problems(self.CANCEL, done) == []

    def test_with_several_held_the_next_to_ring_goes(self) -> None:
        longer = TimerInfo(id="t2", seconds=600, left_s=600)
        held = kept(longer, timer(300))
        done = window(
            "The timer for 5 minutes is cancelled.",
            "timer-cancel",
            before=held,
            after=kept(longer),
            told=("timer t1 cancelled",),
        )
        wrong = window(
            "The timer for 10 minutes is cancelled.",
            "timer-cancel",
            before=held,
            after=kept(timer(300)),
            told=("timer t2 cancelled",),
        )

        assert problems(self.CANCEL, done) == []
        assert problems(self.CANCEL, wrong) != []

    def test_the_one_that_rings_goes_before_one_that_is_nearer_to_ringing(self) -> None:
        ringing = timer(600, 2, ringing=True)
        held = kept(ringing, timer(300))
        done = window(
            "The timer for 10 minutes is cancelled.",
            "timer-cancel",
            before=held,
            after=kept(timer(300)),
            told=("timer t2 cancelled",),
        )

        assert problems(self.CANCEL, done) == []

    def test_the_one_named_by_its_length_goes_and_the_others_stay(self) -> None:
        named = case("cancel the ten minute timer", "timer-cancel", duration="ten minute")
        held = kept(timer(300), timer(600, 2))
        done = window(
            "The timer for 10 minutes is cancelled.",
            "timer-cancel",
            before=held,
            after=kept(timer(300)),
            told=("timer t2 cancelled",),
        )

        assert problems(named, done) == []

    def test_a_length_that_nobody_has_is_said_to_have_no_timer(self) -> None:
        named = case("cancel the ten minute timer", "timer-cancel", duration="ten minute")
        done = window(
            "There is no timer for 10 minutes.",
            "timer-cancel",
            before=kept(timer(300)),
            after=kept(timer(300)),
        )

        assert problems(named, done) == []

    def test_a_timer_that_stayed_when_it_should_have_gone_is_said(self) -> None:
        wrong = window(
            "The timer for 5 minutes is cancelled.",
            "timer-cancel",
            before=kept(timer(300)),
            after=kept(timer(300)),
            told=("timer t1 cancelled",),
        )

        assert any("it holds" in found for found in problems(self.CANCEL, wrong))

    def test_several_that_ring_together_go_together(self) -> None:
        held = kept(timer(300, ringing=True), timer(300, 2, ringing=True))
        done = window(
            "2 timers are cancelled.",
            "timer-cancel",
            before=held,
            after=kept(),
            told=("timer t1 cancelled", "timer t2 cancelled"),
        )

        assert problems(self.CANCEL, done) == []


class TestATimerThatWasAskedAbout:
    ASKED = case("how much time is left on the timer", "timer-status")

    def test_nothing_held_is_said_to_be_nothing(self) -> None:
        assert problems(self.ASKED, window("There are no timers.", "timer-status")) == []

    def test_what_is_left_is_said(self) -> None:
        held = kept(timer(300))

        assert (
            problems(
                self.ASKED, window("The timer has 5 minutes left.", "timer-status", before=held)
            )
            == []
        )
        assert (
            problems(
                self.ASKED, window("The timer has 4 minutes left.", "timer-status", before=held)
            )
            != []
        )

    def test_how_many_there_are_is_said_when_there_are_several(self) -> None:
        held = kept(timer(300), timer(600, 2))
        right = window(
            "You have 2 timers: 5 minutes left, 10 minutes left.", "timer-status", before=held
        )
        wrong = window(
            "The timers have 5 minutes left, 10 minutes left.", "timer-status", before=held
        )

        assert problems(self.ASKED, right) == []
        assert problems(self.ASKED, wrong) == [
            "the number of timers (2) is not in 'The timers have 5 minutes left, 10 minutes left.'"
        ]

    def test_a_question_that_changed_something_is_said(self) -> None:
        wrong = window(
            "There are no timers.",
            "timer-status",
            after=kept(timer(300)),
            told=("timer t1 set for 300 s",),
        )

        assert any("a question changed something" in found for found in problems(self.ASKED, wrong))


class TestAnAlarmThatWasSet:
    def right(self) -> Window:
        return window(
            "Alarm set for tomorrow at 7:30 AM.",
            "alarm",
            after=kept(alarm(civil(2026, 10, 14, 7, 30))),
            told=("alarm a1 set for 2026-10-14 07:30",),
        )

    def test_it_is_right_when_the_device_holds_it_for_the_moment_it_works_out_and_said_it(
        self,
    ) -> None:
        assert problems(SEVEN_THIRTY, self.right()) == []

    def test_the_day_that_was_said_is_the_one_the_device_works_out(self) -> None:
        monday = case("set an alarm for monday at eight", "alarm", time="eight", day="monday")
        done = window(
            "Alarm set for Monday at 8 AM.",
            "alarm",
            after=kept(alarm(civil(2026, 10, 19, 8, 0))),
            told=("alarm a1 set for 2026-10-19 08:00",),
        )
        wrong = window(
            "Alarm set for tomorrow at 8 AM.",
            "alarm",
            after=kept(alarm(civil(2026, 10, 14, 8, 0))),
            told=("alarm a1 set for 2026-10-14 08:00",),
        )

        assert problems(monday, done) == []
        assert any("is for 2026-10-14" in found for found in problems(monday, wrong))

    def test_an_alarm_at_another_moment_is_said(self) -> None:
        wrong = window(
            "Alarm set for tomorrow at 7 AM.",
            "alarm",
            after=kept(alarm(TOMORROW_AT_SEVEN)),
            told=("alarm a1 set for 2026-10-14 07:00",),
        )

        assert any("is for 2026-10-14 07:00" in found for found in problems(SEVEN_THIRTY, wrong))

    def test_no_alarm_or_two_are_said(self) -> None:
        none = window("Alarm set for tomorrow at 7:30 AM.", "alarm")
        two = window(
            "Alarm set for tomorrow at 7:30 AM.",
            "alarm",
            after=kept(alarm(civil(2026, 10, 14, 7, 30)), alarm(civil(2026, 10, 14, 7, 30), 2)),
        )

        assert problems(SEVEN_THIRTY, none) == ["it has 0 alarms, and had 0"]
        assert problems(SEVEN_THIRTY, two) == ["it has 2 alarms, and had 0"]

    def test_a_device_that_was_told_something_else_or_a_wrong_sentence_is_said(self) -> None:
        wrong = changed(
            self.right(),
            events={**self.right().events, KITCHEN: ()},
            outcome=ActOutcome(status=ActStatus.DONE, reply="Alarm set.", skill="alarm"),
        )

        found = problems(SEVEN_THIRTY, wrong)
        assert "it was told ()" in found
        assert any("'Alarm set.'" in line for line in found)

    def test_a_timer_that_was_touched_is_said(self) -> None:
        wrong = changed(
            self.right(),
            time={
                **self.right().time,
                KITCHEN: kept(timer(60), alarm(civil(2026, 10, 14, 7, 30))),
            },
        )

        assert problems(SEVEN_THIRTY, wrong) == ["a timer was touched"]


class TestAnAlarmThatWasCancelled:
    CANCEL = case("cancel the alarm", "alarm-cancel")

    def test_the_next_to_ring_goes(self) -> None:
        later = alarm(civil(2026, 10, 14, 9, 0), 2)
        done = window(
            "The alarm for tomorrow at 7 AM is cancelled.",
            "alarm-cancel",
            before=kept(later, alarm(TOMORROW_AT_SEVEN)),
            after=kept(later),
            told=("alarm a1 cancelled",),
        )

        assert problems(self.CANCEL, done) == []

    def test_with_none_held_it_is_said_that_there_is_none(self) -> None:
        assert problems(self.CANCEL, window("There is no alarm to cancel.", "alarm-cancel")) == []

    def test_the_one_named_by_its_hour_goes(self) -> None:
        named = case("cancel the seven thirty alarm", "alarm-cancel", time="seven thirty")
        later = alarm(civil(2026, 10, 14, 9, 0), 2)
        half = alarm(civil(2026, 10, 14, 7, 30))
        done = window(
            "The alarm for tomorrow at 7:30 AM is cancelled.",
            "alarm-cancel",
            before=kept(later, half),
            after=kept(later),
            told=("alarm a1 cancelled",),
        )

        assert problems(named, done) == []

    def test_an_hour_that_nobody_has_is_said_to_have_no_alarm(self) -> None:
        named = case("cancel the seven thirty alarm", "alarm-cancel", time="seven thirty")
        held = kept(alarm(TOMORROW_AT_SEVEN))
        done = window("There is no alarm at 7:30 AM.", "alarm-cancel", before=held, after=held)

        assert problems(named, done) == []

    def test_one_that_rings_goes_before_one_that_is_nearer(self) -> None:
        ringing = alarm(civil(2026, 10, 14, 9, 0), 2, ringing=True)
        held = kept(ringing, alarm(TOMORROW_AT_SEVEN))
        done = window(
            "The alarm for tomorrow at 9 AM is cancelled.",
            "alarm-cancel",
            before=held,
            after=kept(alarm(TOMORROW_AT_SEVEN)),
            told=("alarm a2 cancelled",),
        )

        assert problems(self.CANCEL, done) == []

    def test_an_alarm_that_stayed_when_it_should_have_gone_is_said(self) -> None:
        held = kept(alarm(TOMORROW_AT_SEVEN))
        wrong = window(
            "The alarm for tomorrow at 7 AM is cancelled.",
            "alarm-cancel",
            before=held,
            after=held,
            told=("alarm a1 cancelled",),
        )

        assert any("it holds" in found for found in problems(self.CANCEL, wrong))

    def test_several_that_ring_together_go_together(self) -> None:
        ringing = (
            alarm(civil(2026, 10, 14, 9, 0), 2, ringing=True),
            alarm(TOMORROW_AT_SEVEN, ringing=True),
        )
        done = window(
            "2 alarms are cancelled.",
            "alarm-cancel",
            before=kept(*ringing),
            after=kept(),
            told=("alarm a1 cancelled", "alarm a2 cancelled"),
        )

        assert problems(self.CANCEL, done) == []


class TestAnAlarmThatWasAskedAbout:
    ASKED = case("what alarms do I have", "alarm-status")

    def test_nothing_held_is_said_to_be_nothing(self) -> None:
        assert problems(self.ASKED, window("There are no alarms.", "alarm-status")) == []

    def test_when_it_rings_is_said(self) -> None:
        held = kept(alarm(TOMORROW_AT_SEVEN))
        right = window("Your alarm is set for tomorrow at 7 AM.", "alarm-status", before=held)
        wrong = window("Your alarm is set for tomorrow at 8 AM.", "alarm-status", before=held)

        assert problems(self.ASKED, right) == []
        assert problems(self.ASKED, wrong) != []

    def test_how_many_there_are_is_said_when_there_are_several(self) -> None:
        held = kept(alarm(TOMORROW_AT_SEVEN), alarm(civil(2026, 10, 14, 9, 0), 2))
        right = window(
            "You have 2 alarms: tomorrow at 7 AM, tomorrow at 9 AM.", "alarm-status", before=held
        )
        wrong = window("Alarms: tomorrow at 7 AM, tomorrow at 9 AM.", "alarm-status", before=held)

        assert problems(self.ASKED, right) == []
        assert any("number of alarms (2)" in found for found in problems(self.ASKED, wrong))

    def test_a_question_that_changed_something_is_said(self) -> None:
        wrong = window(
            "There are no alarms.",
            "alarm-status",
            after=kept(alarm(TOMORROW_AT_SEVEN)),
            told=("alarm a1 set for 2026-10-14 07:00",),
        )

        assert any("a question changed something" in found for found in problems(self.ASKED, wrong))


class TestAnAlarmThatWasPutOff:
    SNOOZE = case("snooze the alarm", "alarm-postpone")
    LATER = alarm(civil(2026, 10, 14, 9, 0), 2)

    def test_the_next_to_ring_is_put_off_by_the_snooze_of_the_device(self) -> None:
        done = window(
            "The alarm will ring tomorrow at 7:10 AM.",
            "alarm-postpone",
            before=kept(self.LATER, alarm(TOMORROW_AT_SEVEN)),
            after=kept(self.LATER, alarm(civil(2026, 10, 14, 7, 10))),
            told=("alarm a1 put off to 2026-10-14 07:10",),
        )

        assert problems(self.SNOOZE, done) == []

    def test_by_as_long_as_was_said(self) -> None:
        asked = case("put the alarm off by half an hour", "alarm-postpone", by="half an hour")
        done = window(
            "The alarm will ring tomorrow at 7:30 AM.",
            "alarm-postpone",
            before=kept(alarm(TOMORROW_AT_SEVEN)),
            after=kept(alarm(civil(2026, 10, 14, 7, 30))),
            told=("alarm a1 put off to 2026-10-14 07:30",),
        )

        assert problems(asked, done) == []
        assert problems(self.SNOOZE, done) != []  # that is not the snooze of the device

    def test_one_that_rings_is_put_off_from_now_and_not_from_the_hour_it_rang_at(self) -> None:
        ringing = alarm(civil(2026, 10, 13, 21, 55), ringing=True)
        done = window(
            "The alarm will ring today at 10:10 PM.",
            "alarm-postpone",
            before=kept(ringing),
            after=kept(alarm(civil(2026, 10, 13, 22, 10))),
            told=("alarm a1 put off to 2026-10-13 22:10",),
        )
        from_the_hour = window(
            "The alarm will ring today at 10:05 PM.",
            "alarm-postpone",
            before=kept(ringing),
            after=kept(alarm(civil(2026, 10, 13, 22, 5))),
            told=("alarm a1 put off to 2026-10-13 22:05",),
        )

        assert problems(self.SNOOZE, done) == []
        assert problems(self.SNOOZE, from_the_hour) != []

    def test_several_that_ring_together_are_put_off_together(self) -> None:
        ringing = (
            alarm(civil(2026, 10, 13, 21, 55), ringing=True),
            alarm(civil(2026, 10, 13, 21, 50), 2, ringing=True),
        )
        done = window(
            "2 alarms are put off.",
            "alarm-postpone",
            before=kept(*ringing),
            after=kept(alarm(civil(2026, 10, 13, 22, 10)), alarm(civil(2026, 10, 13, 22, 10), 2)),
            told=(
                "alarm a2 put off to 2026-10-13 22:10",
                "alarm a1 put off to 2026-10-13 22:10",
            ),
        )

        assert problems(self.SNOOZE, done) == []

    def test_with_none_held_it_is_said_that_there_is_none(self) -> None:
        done = window("There is no alarm to change.", "alarm-postpone")

        assert problems(self.SNOOZE, done) == []

    def test_in_italian_it_is_said_in_italian(self) -> None:
        italian = case("rimanda la sveglia", "alarm-postpone")
        done = window(
            "La sveglia suonerà domani alle 7 e 10.",
            "alarm-postpone",
            before=kept(alarm(TOMORROW_AT_SEVEN)),
            after=kept(alarm(civil(2026, 10, 14, 7, 10))),
            told=("alarm a1 put off to 2026-10-14 07:10",),
        )

        assert problems(italian, done) == []

    def test_an_alarm_that_stayed_where_it_was_is_said(self) -> None:
        held = kept(alarm(TOMORROW_AT_SEVEN))
        wrong = window(
            "The alarm will ring tomorrow at 7:10 AM.",
            "alarm-postpone",
            before=held,
            after=held,
            told=("alarm a1 put off to 2026-10-14 07:10",),
        )

        assert any("it holds" in found for found in problems(self.SNOOZE, wrong))

    def test_one_that_was_not_the_next_to_ring_is_said(self) -> None:
        held = kept(self.LATER, alarm(TOMORROW_AT_SEVEN))
        wrong = window(
            "The alarm will ring tomorrow at 9:10 AM.",
            "alarm-postpone",
            before=held,
            after=kept(alarm(civil(2026, 10, 14, 9, 10), 2), alarm(TOMORROW_AT_SEVEN)),
            told=("alarm a2 put off to 2026-10-14 09:10",),
        )

        found = problems(self.SNOOZE, wrong)
        assert any("it holds" in line for line in found)
        assert any("it was told" in line for line in found)

    def test_a_device_that_was_told_something_else_or_a_wrong_sentence_is_said(self) -> None:
        right = window(
            "The alarm will ring tomorrow at 7:10 AM.",
            "alarm-postpone",
            before=kept(alarm(TOMORROW_AT_SEVEN)),
            after=kept(alarm(civil(2026, 10, 14, 7, 10))),
            told=("alarm a1 put off to 2026-10-14 07:10",),
        )
        wrong = changed(
            right,
            events={**right.events, KITCHEN: ()},
            outcome=ActOutcome(status=ActStatus.DONE, reply="Done.", skill="alarm-postpone"),
        )

        found = problems(self.SNOOZE, wrong)
        assert any("it was told ()" in line for line in found)
        assert any("'Done.'" in line for line in found)

    def test_a_timer_that_was_touched_is_said(self) -> None:
        wrong = window(
            "The alarm will ring tomorrow at 7:10 AM.",
            "alarm-postpone",
            before=kept(alarm(TOMORROW_AT_SEVEN), timer(300)),
            after=kept(alarm(civil(2026, 10, 14, 7, 10)), timer(60)),
            told=("alarm a1 put off to 2026-10-14 07:10",),
        )

        assert problems(self.SNOOZE, wrong) == ["a timer was touched"]

    def test_a_case_with_a_length_that_is_not_one_is_the_corpus_s_mistake(self) -> None:
        broken = case("snooze it for a while", "alarm-postpone", by="a while")

        assert problems(broken, window("Done.", "alarm-postpone")) == [
            "the case has a length that is not one: 'a while'"
        ]


class TestAnAlarmThatWasMoved:
    MOVE = case("move the alarm to eight", "alarm-change", time="eight")
    LATER = alarm(civil(2026, 10, 14, 9, 0), 2)

    def test_the_next_to_ring_is_moved_to_the_next_time_the_new_hour_comes(self) -> None:
        done = window(
            "The alarm will ring tomorrow at 8 AM.",
            "alarm-change",
            before=kept(alarm(TOMORROW_AT_SEVEN)),
            after=kept(alarm(civil(2026, 10, 14, 8, 0))),
            told=("alarm a1 moved to 2026-10-14 08:00",),
        )

        assert problems(self.MOVE, done) == []

    def test_the_day_that_was_said_is_the_one_the_device_works_out(self) -> None:
        asked = case(
            "sposta la sveglia a lunedì alle sei", "alarm-change", time="sei", day="lunedì"
        )
        done = window(
            "La sveglia suonerà lunedì alle 6.",
            "alarm-change",
            before=kept(alarm(TOMORROW_AT_SEVEN)),
            after=kept(alarm(civil(2026, 10, 19, 6, 0))),
            told=("alarm a1 moved to 2026-10-19 06:00",),
        )

        assert problems(asked, done) == []

    def test_the_one_that_was_named_is_the_one_that_moves(self) -> None:
        named = case(
            "move the seven o'clock alarm to eight",
            "alarm-change",
            time="eight",
            **{"from": "seven"},
        )
        done = window(
            "The alarm will ring tomorrow at 8 AM.",
            "alarm-change",
            before=kept(self.LATER, alarm(TOMORROW_AT_SEVEN)),
            after=kept(self.LATER, alarm(civil(2026, 10, 14, 8, 0))),
            told=("alarm a1 moved to 2026-10-14 08:00",),
        )
        wrong = window(
            "The alarm will ring tomorrow at 8 AM.",
            "alarm-change",
            before=kept(self.LATER, alarm(TOMORROW_AT_SEVEN)),
            after=kept(alarm(civil(2026, 10, 14, 8, 0), 2), alarm(TOMORROW_AT_SEVEN)),
            told=("alarm a2 moved to 2026-10-14 08:00",),
        )

        assert problems(named, done) == []
        assert problems(named, wrong) != []

    def test_an_hour_that_nobody_has_is_said_to_have_no_alarm(self) -> None:
        named = case(
            "move the seven o'clock alarm to eight",
            "alarm-change",
            time="eight",
            **{"from": "seven"},
        )
        held = kept(self.LATER)
        done = window("There is no alarm at 7 AM.", "alarm-change", before=held, after=held)

        assert problems(named, done) == []

    def test_the_minute_of_the_hour_that_nobody_has_is_said_too(self) -> None:
        named = case(
            "move the seven thirty alarm to eight",
            "alarm-change",
            time="eight",
            **{"from": "seven thirty"},
        )
        held = kept(self.LATER)
        done = window("There is no alarm at 7:30 AM.", "alarm-change", before=held, after=held)
        without_the_minute = window(
            "There is no alarm at 7 AM.", "alarm-change", before=held, after=held
        )

        assert problems(named, done) == []
        assert problems(named, without_the_minute) != []

    def test_with_none_held_it_is_said_that_there_is_none(self) -> None:
        done = window("There is no alarm to change.", "alarm-change")

        assert problems(self.MOVE, done) == []

    def test_one_that_rings_moves_before_one_that_is_nearer_and_is_quiet_after(self) -> None:
        ringing = alarm(civil(2026, 10, 14, 9, 0), 2, ringing=True)
        held = kept(ringing, alarm(TOMORROW_AT_SEVEN))
        done = window(
            "The alarm will ring tomorrow at 8 AM.",
            "alarm-change",
            before=held,
            after=kept(alarm(civil(2026, 10, 14, 8, 0), 2), alarm(TOMORROW_AT_SEVEN)),
            told=("alarm a2 moved to 2026-10-14 08:00",),
        )
        still_ringing = window(
            "The alarm will ring tomorrow at 8 AM.",
            "alarm-change",
            before=held,
            after=kept(alarm(civil(2026, 10, 14, 8, 0), 2, ringing=True), alarm(TOMORROW_AT_SEVEN)),
            told=("alarm a2 moved to 2026-10-14 08:00",),
        )

        assert problems(self.MOVE, done) == []
        assert any("it holds" in found for found in problems(self.MOVE, still_ringing))

    def test_an_alarm_that_stayed_where_it_was_or_the_wrong_sentence_is_said(self) -> None:
        held = kept(alarm(TOMORROW_AT_SEVEN))
        wrong = window(
            "Done.",
            "alarm-change",
            before=held,
            after=held,
            told=("alarm a1 moved to 2026-10-14 08:00",),
        )

        found = problems(self.MOVE, wrong)
        assert any("it holds" in line for line in found)
        assert any("'Done.'" in line for line in found)

    def test_a_timer_that_was_touched_is_said(self) -> None:
        wrong = window(
            "The alarm will ring tomorrow at 8 AM.",
            "alarm-change",
            before=kept(alarm(TOMORROW_AT_SEVEN), timer(300)),
            after=kept(alarm(civil(2026, 10, 14, 8, 0)), timer(60)),
            told=("alarm a1 moved to 2026-10-14 08:00",),
        )

        assert problems(self.MOVE, wrong) == ["a timer was touched"]

    @pytest.mark.parametrize(
        ("broken", "wanted"),
        [
            (case("x", "alarm-change", time="soon"), "the case has an hour that is not one"),
            (
                case("x", "alarm-change", time="eight", **{"from": "soon"}),
                "the case has an hour that is not one",
            ),
            (
                case("x", "alarm-change", time="seven", day="today"),
                "the case asks for a moment that cannot be (past)",
            ),
        ],
    )
    def test_a_case_that_cannot_come_out_is_the_corpus_s_mistake(
        self, broken: PlanCase, wanted: str
    ) -> None:
        assert problems(broken, window("Done.", "alarm-change")) == [wanted]


class TestWhatRingsThatWasStopped:
    STOP = case("stop", "stop")

    def test_with_nothing_held_it_is_said_that_nothing_rings(self) -> None:
        assert problems(self.STOP, window("Nothing is ringing.", "stop")) == []

    def test_what_is_held_and_does_not_ring_is_left_alone(self) -> None:
        held = kept(timer(300), alarm(TOMORROW_AT_SEVEN, 2))
        done = window("Nothing is ringing.", "stop", before=held, after=held)
        cancelled = window(
            "Nothing is ringing.", "stop", before=held, after=kept(), told=("timer t1 stopped",)
        )

        assert problems(self.STOP, done) == []
        assert problems(self.STOP, cancelled) != []

    def test_what_rings_is_stopped_and_what_does_not_stays(self) -> None:
        held = kept(
            timer(60, ringing=True),
            alarm(civil(2026, 10, 13, 22, 0), 2, ringing=True),
            timer(600, 3),
        )
        done = window(
            "Stopped.",
            "stop",
            before=held,
            after=kept(timer(600, 3)),
            told=("timer t1 stopped", "alarm a2 stopped"),
        )

        assert problems(self.STOP, done) == []

    def test_in_italian_it_is_said_in_italian(self) -> None:
        italian = case("basta", "stop")
        held = kept(timer(60, ringing=True))
        done = window("Fermato.", "stop", before=held, after=kept(), told=("timer t1 stopped",))
        nothing = window("Non sta suonando niente.", "stop")

        assert problems(italian, done) == []
        assert problems(italian, nothing) == []
        assert problems(self.STOP, nothing) != []  # to a person who spoke English

    def test_a_ring_that_was_left_is_said(self) -> None:
        held = kept(timer(60, ringing=True), alarm(civil(2026, 10, 13, 22, 0), 2, ringing=True))
        wrong = window(
            "Stopped.",
            "stop",
            before=held,
            after=kept(alarm(civil(2026, 10, 13, 22, 0), 2, ringing=True)),
            told=("timer t1 stopped",),
        )

        found = problems(self.STOP, wrong)
        assert any("it holds the alarms" in line for line in found)
        assert any("it was told" in line for line in found)

    def test_a_wrong_sentence_is_said(self) -> None:
        held = kept(timer(60, ringing=True))
        wrong = window(
            "Nothing is ringing.", "stop", before=held, after=kept(), told=("timer t1 stopped",)
        )

        [found] = problems(self.STOP, wrong)
        assert "'Nothing is ringing.'" in found
        assert "'Stopped.'" in found


@pytest.mark.parametrize("speaker", [KITCHEN, DISPLAY, PHONE])
def test_whichever_device_was_spoken_to_is_the_one_that_is_looked_at(speaker: str) -> None:
    a_case = PlanCase(
        "set a timer for five minutes",
        speaker,
        "timer",
        ActStatus.DONE,
        None,
        {"duration": "five minutes"},
    )
    done = window(
        "Timer set for 5 minutes.",
        "timer",
        after=kept(timer(300)),
        told=("timer t1 set for 300 s",),
        speaker=speaker,
    )

    assert problems(a_case, done) == []
