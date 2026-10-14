"""The timers and the alarms of a device (ADR 0029): kept on a clock of its own, rung by themselves,
said again until somebody stops them, and lost when the program stops."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from factotum.client_core.testing import FakeClock, FakeVoiceOutput, RecordingRenderer, settle
from factotum.client_core.timekeeping import (
    DEFAULT_SNOOZE_S,
    RING_EVERY_S,
    RING_FOR_S,
    Ring,
    SystemClock,
    Timekeeper,
)
from factotum.contracts.audio import AudioPayload, wav_of
from factotum.contracts.commands import (
    AlarmDay,
    CancelAlarm,
    CancelTimer,
    MoveAlarm,
    PostponeAlarm,
    SetAlarm,
    SetTimer,
    StopRinging,
    Weekday,
)
from factotum.contracts.timekeeping import (
    MAX_ALARMS,
    MAX_TIMERS,
    AlarmsCancelled,
    AlarmSet,
    AlarmsMoved,
    AlarmsPostponed,
    RingsStopped,
    TimersCancelled,
    TimerSet,
    TimeState,
    answer_in,
)


def civil(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute)  # noqa: DTZ001 - civil time


EVENING = civil(2026, 10, 13, 22, 0)
"""A Tuesday."""


def clip(fill: int = 1) -> AudioPayload:
    return wav_of(fill.to_bytes(2, "little", signed=True) * 50, 22_050)


class Sayer:
    """What a connected client lends the keeper: it remembers which rings it was asked to say, and
    says each for as long as a test lets it."""

    def __init__(self) -> None:
        self.said: list[Ring] = []
        self.saying = asyncio.Event()
        self.hold: asyncio.Event | None = None
        self.cut = 0

    async def __call__(self, ring: Ring) -> None:
        self.said.append(ring)
        self.saying.set()
        if self.hold is not None:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.cut += 1
                raise

    def words(self) -> list[str]:
        return [ring.text for ring in self.said]


class Keeper:
    """A keeper on a clock that a test moves, with a sayer that remembers."""

    def __init__(self, wall: datetime = EVENING, snooze_s: int = DEFAULT_SNOOZE_S) -> None:
        self.clock = FakeClock(wall)
        self.shown = RecordingRenderer()
        self.sayer = Sayer()
        self.timekeeper = Timekeeper(self.shown, clock=self.clock, snooze_s=snooze_s)
        self.timekeeper.attach(self.sayer)

    async def timer(self, seconds: int, text: str = "Timer.", fill: int = 1) -> TimerSet:
        result = await self.timekeeper.execute(
            SetTimer(seconds=seconds, text=text, audio=clip(fill))
        )
        assert result.succeeded
        answer = answer_in(TimerSet, result.data)
        assert answer is not None
        return answer

    async def alarm(
        self, hour: int, minute: int, day: AlarmDay | None = None, text: str = "Alarm."
    ) -> AlarmSet:
        command = SetAlarm(hour=hour, minute=minute, day=day or AlarmDay(), text=text, audio=clip())
        result = await self.timekeeper.execute(command)
        assert result.succeeded
        answer = answer_in(AlarmSet, result.data)
        assert answer is not None
        return answer

    async def cancel_timer(self, seconds: int | None = None) -> TimersCancelled:
        result = await self.timekeeper.execute(CancelTimer(seconds=seconds))
        answer = answer_in(TimersCancelled, result.data)
        assert answer is not None
        return answer

    async def cancel_alarm(self, at: tuple[int, int] | None = None) -> AlarmsCancelled:
        hour, minute = at or (None, None)
        result = await self.timekeeper.execute(CancelAlarm(hour=hour, minute=minute))
        answer = answer_in(AlarmsCancelled, result.data)
        assert answer is not None
        return answer

    async def postpone(
        self, seconds: int | None = None, at: tuple[int, int] | None = None
    ) -> AlarmsPostponed:
        hour, minute = at or (None, None)
        result = await self.timekeeper.execute(
            PostponeAlarm(seconds=seconds, hour=hour, minute=minute)
        )
        answer = answer_in(AlarmsPostponed, result.data)
        assert answer is not None
        return answer

    async def move(
        self,
        hour: int,
        minute: int,
        day: AlarmDay | None = None,
        was: tuple[int, int] | None = None,
    ) -> AlarmsMoved:
        from_hour, from_minute = was or (None, None)
        command = MoveAlarm(
            hour=hour,
            minute=minute,
            day=day or AlarmDay(),
            from_hour=from_hour,
            from_minute=from_minute,
        )
        answer = answer_in(AlarmsMoved, (await self.timekeeper.execute(command)).data)
        assert answer is not None
        return answer

    async def stop(self) -> RingsStopped:
        answer = answer_in(RingsStopped, (await self.timekeeper.execute(StopRinging())).data)
        assert answer is not None
        return answer

    def state(self) -> TimeState:
        state = answer_in(TimeState, self.timekeeper.state())
        assert state is not None
        return state


@pytest.fixture
async def keeper() -> AsyncIterator[Keeper]:
    made = Keeper()
    yield made
    await made.timekeeper.close()


class TestATimer:
    async def test_it_is_kept_with_its_length_and_what_is_left_of_it(self, keeper: Keeper) -> None:
        answer = await keeper.timer(300)

        assert answer.timer is not None
        assert (answer.timer.seconds, answer.timer.left_s, answer.timer.ringing) == (
            300,
            300,
            False,
        )
        await keeper.clock.advance(26)
        assert keeper.state().timers[0].left_s == 274

    async def test_what_is_left_is_never_less_than_what_is_left(self, keeper: Keeper) -> None:
        """A timer with half a second gone has five minutes left for a person who counts whole
        seconds, and not less: it is rounded up."""
        await keeper.timer(300)

        await keeper.clock.advance(0.5)
        assert keeper.state().timers[0].left_s == 300
        await keeper.clock.advance(0.5)
        assert keeper.state().timers[0].left_s == 299

    async def test_it_does_not_ring_before_it_is_up(self, keeper: Keeper) -> None:
        await keeper.timer(300)

        await keeper.clock.advance(299)

        assert keeper.sayer.said == []
        assert not keeper.state().timers[0].ringing

    async def test_it_rings_when_it_is_up_with_the_clip_it_was_given(self, keeper: Keeper) -> None:
        await keeper.timer(300, "Five minutes are up.", fill=5)

        await keeper.clock.advance(300)

        assert keeper.sayer.said == [Ring("Five minutes are up.", clip(5))]
        assert keeper.state().timers[0].ringing
        assert keeper.state().timers[0].left_s == 0

    async def test_it_rings_again_and_again_until_it_is_stopped(self, keeper: Keeper) -> None:
        await keeper.timer(60)

        await keeper.clock.advance(60)
        assert len(keeper.sayer.said) == 1
        await keeper.clock.advance(RING_EVERY_S - 1)
        assert len(keeper.sayer.said) == 1
        await keeper.clock.advance(1)
        assert len(keeper.sayer.said) == 2
        await keeper.clock.advance(RING_EVERY_S)
        assert len(keeper.sayer.said) == 3

    async def test_it_stops_ringing_for_good_after_a_while_and_says_so(
        self, keeper: Keeper
    ) -> None:
        await keeper.timer(60)
        await keeper.clock.advance(60)

        await keeper.clock.advance(RING_FOR_S)

        assert keeper.state().timers == ()
        assert keeper.shown.of("notice") == ["t1 rang for 180 s and nobody stopped it"]
        said = len(keeper.sayer.said)
        await keeper.clock.advance(RING_FOR_S)
        assert len(keeper.sayer.said) == said
        assert keeper.clock.sleepers == 0  # nothing is left to look at

    async def test_a_timer_that_is_set_when_all_have_rung_out_rings_too(
        self, keeper: Keeper
    ) -> None:
        """Nothing is left to look at, the keeper stops looking, and a new timer starts it again."""
        await keeper.timer(60)
        await keeper.clock.advance(60 + RING_FOR_S)
        assert keeper.state().timers == ()
        assert keeper.clock.sleepers == 0
        said = len(keeper.sayer.said)

        await keeper.timer(30, "Second.")
        await keeper.clock.advance(30)

        assert keeper.sayer.words()[said:] == ["Second."]

    async def test_a_ring_waits_for_the_one_before_it_to_be_said(self, keeper: Keeper) -> None:
        keeper.sayer.hold = asyncio.Event()
        await keeper.timer(60)
        await keeper.clock.advance(60)

        await keeper.clock.advance(RING_EVERY_S * 3)  # the first is still being said
        assert len(keeper.sayer.said) == 1

        keeper.sayer.hold.set()
        await keeper.clock.advance(RING_EVERY_S + 2)  # it is said again that long after it was said
        assert len(keeper.sayer.said) == 2

    async def test_two_that_are_up_together_are_two_rings_one_after_the_other(
        self, keeper: Keeper
    ) -> None:
        keeper.sayer.hold = asyncio.Event()
        await keeper.timer(60, "First.")
        await keeper.timer(60, "Second.")

        await keeper.clock.advance(60)
        assert keeper.sayer.words() == ["First."]  # the second waits for it to be said
        keeper.sayer.hold.set()
        await keeper.clock.advance(1)

        assert keeper.sayer.words()[:2] == ["First.", "Second."]

    async def test_several_are_kept_and_the_state_lists_them_in_the_order_they_are_up(
        self, keeper: Keeper
    ) -> None:
        await keeper.timer(600)
        await keeper.timer(60)
        await keeper.timer(300)

        assert [timer.seconds for timer in keeper.state().timers] == [60, 300, 600]
        assert [timer.id for timer in keeper.state().timers] == ["t2", "t3", "t1"]

    async def test_there_is_a_limit_to_how_many_a_device_keeps(self, keeper: Keeper) -> None:
        for _ in range(MAX_TIMERS):
            assert (await keeper.timer(60)).timer is not None

        refused = await keeper.timer(60)

        assert refused.timer is None
        assert refused.reason == "too_many"
        assert len(keeper.state().timers) == MAX_TIMERS

    async def test_it_is_kept_on_the_clock_that_only_goes_forward(self, keeper: Keeper) -> None:
        """The wall may be set to another hour while a timer runs; the time it was set for is
        the time that goes by."""
        await keeper.timer(300)

        keeper.clock.set_wall(civil(2026, 10, 14, 3, 0))
        await keeper.clock.advance(100)

        assert keeper.sayer.said == []
        assert keeper.state().timers[0].left_s == 200
        await keeper.clock.advance(200)
        assert len(keeper.sayer.said) == 1

    async def test_the_device_shows_that_it_was_set_and_that_it_is_up(self, keeper: Keeper) -> None:
        await keeper.timer(90)
        await keeper.clock.advance(90)

        assert keeper.shown.of("event") == ["timer t1 set for 90 s", "timer t1 is up"]


class TestCancellingATimer:
    async def test_the_only_one_is_cancelled_and_never_rings(self, keeper: Keeper) -> None:
        await keeper.timer(60)

        gone = await keeper.cancel_timer()

        assert [timer.seconds for timer in gone.cancelled] == [60]
        assert gone.remaining == 0
        await keeper.clock.advance(600)
        assert keeper.sayer.said == []
        assert keeper.shown.of("event")[-1] == "timer t1 cancelled"

    async def test_the_one_that_was_named_by_its_length(self, keeper: Keeper) -> None:
        await keeper.timer(60)
        await keeper.timer(300)

        gone = await keeper.cancel_timer(300)

        assert [timer.seconds for timer in gone.cancelled] == [300]
        assert [timer.seconds for timer in keeper.state().timers] == [60]
        assert gone.remaining == 1

    async def test_with_none_named_the_next_to_ring_is_cancelled(self, keeper: Keeper) -> None:
        await keeper.timer(600)
        await keeper.timer(60)
        await keeper.timer(300)

        gone = await keeper.cancel_timer()

        assert [timer.seconds for timer in gone.cancelled] == [60]
        assert [timer.seconds for timer in keeper.state().timers] == [300, 600]

    async def test_the_one_that_rings_is_cancelled_before_one_that_has_not_rung(
        self, keeper: Keeper
    ) -> None:
        await keeper.timer(600)
        await keeper.timer(60)
        await keeper.clock.advance(60)

        gone = await keeper.cancel_timer()

        assert [timer.seconds for timer in gone.cancelled] == [60]
        assert [timer.seconds for timer in keeper.state().timers] == [600]

    async def test_the_one_that_was_named_is_cancelled_even_when_another_is_ringing(
        self, keeper: Keeper
    ) -> None:
        await keeper.timer(600)
        await keeper.timer(60)
        await keeper.clock.advance(60)

        gone = await keeper.cancel_timer(600)

        assert [timer.seconds for timer in gone.cancelled] == [600]
        assert keeper.state().timers[0].ringing

    async def test_all_that_ring_are_stopped_together(self, keeper: Keeper) -> None:
        await keeper.timer(60)
        await keeper.timer(60)
        await keeper.timer(600)
        await keeper.clock.advance(60)

        gone = await keeper.cancel_timer()

        assert len(gone.cancelled) == 2
        assert [timer.seconds for timer in keeper.state().timers] == [600]

    async def test_there_is_nothing_to_cancel_when_nothing_matches(self, keeper: Keeper) -> None:
        await keeper.timer(60)

        gone = await keeper.cancel_timer(300)

        assert gone.cancelled == ()
        assert gone.remaining == 1
        assert (await Keeper().cancel_timer()).cancelled == ()

    async def test_the_ring_that_is_being_said_is_cut(self, keeper: Keeper) -> None:
        keeper.sayer.hold = asyncio.Event()
        await keeper.timer(60)
        await keeper.clock.advance(60)
        await asyncio.wait_for(keeper.sayer.saying.wait(), 2)

        await keeper.cancel_timer()
        await settle()

        assert keeper.sayer.cut == 1

    async def test_the_ring_that_is_being_said_for_another_timer_is_not(
        self, keeper: Keeper
    ) -> None:
        keeper.sayer.hold = asyncio.Event()
        await keeper.timer(60)
        await keeper.timer(600)
        await keeper.clock.advance(60)
        await asyncio.wait_for(keeper.sayer.saying.wait(), 2)

        await keeper.cancel_timer(600)
        await settle()

        assert keeper.sayer.cut == 0


class TestAnAlarm:
    async def test_it_is_set_for_the_moment_the_device_works_out(self, keeper: Keeper) -> None:
        answer = await keeper.alarm(7, 30)

        assert answer.now == EVENING
        assert answer.alarm is not None
        assert answer.alarm.at == civil(2026, 10, 14, 7, 30)
        assert keeper.state().alarms == (answer.alarm,)

    async def test_the_day_is_worked_out_from_the_calendar_of_the_device(
        self, keeper: Keeper
    ) -> None:
        monday = await keeper.alarm(7, 0, AlarmDay(weekday=Weekday.MONDAY))
        date = await keeper.alarm(7, 0, AlarmDay(day=15, month=11))
        in_days = await keeper.alarm(7, 0, AlarmDay(in_days=2))

        assert [a.alarm.at for a in (monday, date, in_days) if a.alarm] == [
            civil(2026, 10, 19, 7, 0),
            civil(2026, 11, 15, 7, 0),
            civil(2026, 10, 15, 7, 0),
        ]

    async def test_a_moment_that_has_gone_by_is_not_an_alarm_and_the_device_says_so(
        self, keeper: Keeper
    ) -> None:
        refused = await keeper.alarm(7, 0, AlarmDay(in_days=0))

        assert refused.alarm is None
        assert refused.reason == "past"
        assert refused.now == EVENING
        assert keeper.state().alarms == ()

    async def test_a_date_that_never_comes_is_not_an_alarm_either(self, keeper: Keeper) -> None:
        refused = await keeper.alarm(7, 0, AlarmDay(day=31, month=4))

        assert refused.reason == "no_such_day"

    async def test_there_is_a_limit_to_how_many_a_device_keeps(self, keeper: Keeper) -> None:
        for _ in range(MAX_ALARMS):
            assert (await keeper.alarm(7, 0)).alarm is not None

        assert (await keeper.alarm(7, 0)).reason == "too_many"

    async def test_it_rings_at_the_hour_on_the_wall(self, keeper: Keeper) -> None:
        await keeper.alarm(23, 30, text="Wake up.")

        await keeper.clock.advance(90 * 60 - 1)
        assert keeper.sayer.said == []
        await keeper.clock.advance(1)

        assert keeper.sayer.words() == ["Wake up."]
        assert keeper.state().alarms[0].ringing
        assert keeper.shown.of("event")[-1] == "alarm a1 is ringing"

    async def test_it_rings_again_until_it_is_stopped_or_has_rung_enough(
        self, keeper: Keeper
    ) -> None:
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)

        await keeper.clock.advance(RING_EVERY_S)
        assert len(keeper.sayer.said) == 2
        await keeper.clock.advance(RING_FOR_S)

        assert keeper.state().alarms == ()
        assert keeper.shown.of("notice") == ["a1 rang for 180 s and nobody stopped it"]

    async def test_it_goes_by_the_wall_and_not_by_the_time_that_has_gone_by(
        self, keeper: Keeper
    ) -> None:
        """An alarm for seven rings when the wall says seven, whatever the clock did in between:
        the wall is set forward (a journey, summer time) and the hour is there."""
        await keeper.alarm(7, 0)

        keeper.clock.set_wall(civil(2026, 10, 14, 7, 0))
        await keeper.clock.advance(1)

        assert len(keeper.sayer.said) == 1

    async def test_it_does_not_ring_for_a_wall_that_was_set_back(self, keeper: Keeper) -> None:
        await keeper.alarm(23, 0)

        keeper.clock.set_wall(civil(2026, 10, 13, 12, 0))
        await keeper.clock.advance(10 * 3600 + 58 * 60)  # 22:58 on the wall

        assert keeper.sayer.said == []
        await keeper.clock.advance(120)  # and 23:00 comes
        assert len(keeper.sayer.said) == 1

    async def test_the_state_lists_them_in_the_order_they_ring(self, keeper: Keeper) -> None:
        await keeper.alarm(9, 0)
        await keeper.alarm(7, 0)

        assert [alarm.at.hour for alarm in keeper.state().alarms] == [7, 9]


class TestCancellingAnAlarm:
    async def test_the_one_named_by_its_hour(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)
        await keeper.alarm(8, 30)

        gone = await keeper.cancel_alarm((8, 30))

        assert [alarm.at.hour for alarm in gone.cancelled] == [8]
        assert gone.remaining == 1
        assert gone.now == EVENING
        assert [alarm.at.hour for alarm in keeper.state().alarms] == [7]

    async def test_with_none_named_the_next_to_ring(self, keeper: Keeper) -> None:
        await keeper.alarm(9, 0)
        await keeper.alarm(7, 0)

        gone = await keeper.cancel_alarm()

        assert [alarm.at.hour for alarm in gone.cancelled] == [7]

    async def test_the_one_that_rings_is_stopped_first(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)
        await keeper.alarm(23, 0)
        await keeper.clock.advance(3600)

        gone = await keeper.cancel_alarm()

        assert [alarm.at.hour for alarm in gone.cancelled] == [23]
        assert [alarm.at.hour for alarm in keeper.state().alarms] == [7]
        await keeper.clock.advance(RING_FOR_S)
        assert len(keeper.sayer.said) == 1

    async def test_an_hour_that_no_alarm_has_cancels_nothing(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)

        gone = await keeper.cancel_alarm((7, 1))

        assert gone.cancelled == ()
        assert gone.remaining == 1


class TestPuttingAnAlarmOff:
    async def test_one_that_rings_is_silenced_and_rings_again_after_the_snooze_of_the_device(
        self, keeper: Keeper
    ) -> None:
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)
        assert len(keeper.sayer.said) == 1

        put_off = await keeper.postpone()

        [alarm] = put_off.postponed
        assert (alarm.at, alarm.ringing) == (civil(2026, 10, 13, 22, 15), False)
        assert put_off.now == civil(2026, 10, 13, 22, 5)
        await keeper.clock.advance(599)
        assert len(keeper.sayer.said) == 1  # it is quiet for the whole of the snooze
        await keeper.clock.advance(1)
        assert len(keeper.sayer.said) == 2

    async def test_the_snooze_counts_from_now_and_not_from_the_hour_it_rang_at(
        self, keeper: Keeper
    ) -> None:
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60 + 30)  # it has been ringing for half a minute

        put_off = await keeper.postpone()

        assert put_off.postponed[0].at == civil(2026, 10, 13, 22, 15).replace(second=30)

    async def test_by_as_long_as_a_person_said(self, keeper: Keeper) -> None:
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)

        put_off = await keeper.postpone(seconds=300)

        assert put_off.postponed[0].at == civil(2026, 10, 13, 22, 10)

    async def test_by_the_snooze_that_the_device_was_given_when_nobody_said(self) -> None:
        keeper = Keeper(snooze_s=120)
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)

        put_off = await keeper.postpone()

        assert put_off.postponed[0].at == civil(2026, 10, 13, 22, 7)
        await keeper.timekeeper.close()

    def test_the_snooze_of_a_device_that_was_given_none_is_ten_minutes(self) -> None:
        assert DEFAULT_SNOOZE_S == 600

    async def test_one_that_has_not_rung_is_moved_later_by_that_much(self, keeper: Keeper) -> None:
        await keeper.alarm(23, 0)

        put_off = await keeper.postpone(seconds=1800)

        assert put_off.postponed[0].at == civil(2026, 10, 13, 23, 30)
        await keeper.clock.advance(3600)
        assert keeper.sayer.said == []  # it did not ring at eleven
        await keeper.clock.advance(1800)
        assert len(keeper.sayer.said) == 1

    async def test_with_none_named_the_next_to_ring_is_put_off(self, keeper: Keeper) -> None:
        await keeper.alarm(9, 0)
        await keeper.alarm(7, 0)

        put_off = await keeper.postpone()

        assert [alarm.at.hour for alarm in put_off.postponed] == [7]
        assert [(a.at.hour, a.at.minute) for a in keeper.state().alarms] == [(7, 10), (9, 0)]

    async def test_the_one_named_by_its_hour(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)
        await keeper.alarm(8, 0)

        put_off = await keeper.postpone(seconds=600, at=(8, 0))

        assert [(a.at.hour, a.at.minute) for a in put_off.postponed] == [(8, 10)]
        assert [(a.at.hour, a.at.minute) for a in keeper.state().alarms] == [(7, 0), (8, 10)]

    async def test_the_one_that_rings_is_put_off_before_one_that_has_not_rung(
        self, keeper: Keeper
    ) -> None:
        await keeper.alarm(22, 5)
        await keeper.alarm(23, 0)
        await keeper.clock.advance(5 * 60)

        put_off = await keeper.postpone()

        assert [alarm.id for alarm in put_off.postponed] == ["a1"]
        assert [(a.at.hour, a.at.minute) for a in keeper.state().alarms] == [(22, 15), (23, 0)]

    async def test_all_that_ring_are_put_off_together(self, keeper: Keeper) -> None:
        await keeper.alarm(22, 5)
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)

        put_off = await keeper.postpone()

        assert len(put_off.postponed) == 2
        assert [alarm.ringing for alarm in keeper.state().alarms] == [False, False]

    async def test_there_is_nothing_to_put_off_when_nothing_matches(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)

        none_named = await keeper.postpone(at=(7, 1))

        assert none_named.postponed == ()
        assert keeper.state().alarms[0].at == civil(2026, 10, 14, 7, 0)
        assert (await Keeper().postpone()).postponed == ()

    async def test_the_ring_that_is_being_said_is_cut(self, keeper: Keeper) -> None:
        keeper.sayer.hold = asyncio.Event()
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)
        await asyncio.wait_for(keeper.sayer.saying.wait(), 2)

        await keeper.postpone()
        await settle()

        assert keeper.sayer.cut == 1

    async def test_the_snooze_is_not_counted_as_ringing(self, keeper: Keeper) -> None:
        """It has not given up when it comes back: the three minutes that it rings for begin again
        when it does, and are not the ones that went by before it was put off."""
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)

        await keeper.postpone()
        await keeper.clock.advance(599)

        assert len(keeper.state().alarms) == 1  # it did not give up while it was quiet
        await keeper.clock.advance(1 + RING_FOR_S - 1)
        assert len(keeper.state().alarms) == 1  # and rings for as long as an alarm does
        await keeper.clock.advance(1)
        assert keeper.state().alarms == ()
        assert keeper.shown.of("notice") == ["a1 rang for 180 s and nobody stopped it"]

    async def test_the_device_shows_that_it_was_put_off(self, keeper: Keeper) -> None:
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)

        await keeper.postpone()

        assert keeper.shown.of("event")[-1] == "alarm a1 put off to 2026-10-13 22:15"


class TestMovingAnAlarm:
    async def test_the_next_to_ring_is_moved_to_the_next_time_the_new_hour_comes(
        self, keeper: Keeper
    ) -> None:
        await keeper.alarm(7, 0)

        moved = await keeper.move(8, 30)

        assert moved.reason is None
        assert [alarm.at for alarm in moved.moved] == [civil(2026, 10, 14, 8, 30)]
        assert [alarm.at for alarm in keeper.state().alarms] == [civil(2026, 10, 14, 8, 30)]
        await keeper.clock.advance(9 * 3600)  # to seven: it does not ring there any more
        assert keeper.sayer.said == []
        await keeper.clock.advance(5400)
        assert len(keeper.sayer.said) == 1

    async def test_the_day_is_worked_out_from_the_calendar_of_the_device(
        self, keeper: Keeper
    ) -> None:
        await keeper.alarm(7, 0)

        moved = await keeper.move(8, 0, AlarmDay(weekday=Weekday.MONDAY))

        assert [alarm.at for alarm in moved.moved] == [civil(2026, 10, 19, 8, 0)]

    async def test_the_one_named_by_its_hour_is_the_one_that_moves(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)
        await keeper.alarm(9, 0)

        moved = await keeper.move(6, 30, was=(9, 0))

        assert [alarm.id for alarm in moved.moved] == ["a2"]
        assert [(a.at.hour, a.at.minute) for a in keeper.state().alarms] == [(6, 30), (7, 0)]

    async def test_the_one_that_rings_moves_before_one_that_has_not_rung(
        self, keeper: Keeper
    ) -> None:
        await keeper.alarm(22, 5)
        await keeper.alarm(23, 0)
        await keeper.clock.advance(5 * 60)

        moved = await keeper.move(6, 0)

        assert [alarm.id for alarm in moved.moved] == ["a1"]
        assert not any(alarm.ringing for alarm in keeper.state().alarms)
        await keeper.clock.advance(RING_EVERY_S * 4)
        assert len(keeper.sayer.said) == 1  # it is quiet: it rings at six

    async def test_a_moment_that_has_gone_by_moves_nothing(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)

        refused = await keeper.move(7, 0, AlarmDay(in_days=0))

        assert (refused.moved, refused.reason, refused.now) == ((), "past", EVENING)
        assert [alarm.at for alarm in keeper.state().alarms] == [civil(2026, 10, 14, 7, 0)]

    async def test_a_date_that_never_comes_moves_nothing(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)

        refused = await keeper.move(7, 0, AlarmDay(day=31, month=4))

        assert refused.reason == "no_such_day"
        assert [alarm.at for alarm in keeper.state().alarms] == [civil(2026, 10, 14, 7, 0)]

    async def test_with_no_alarm_there_is_none_to_move(self, keeper: Keeper) -> None:
        none_at_all = await keeper.move(8, 0)
        await keeper.alarm(7, 0)
        none_named = await keeper.move(8, 0, was=(7, 1))

        assert (none_at_all.reason, none_named.reason) == ("none", "none")
        assert none_at_all.moved == () == none_named.moved
        assert [alarm.at for alarm in keeper.state().alarms] == [civil(2026, 10, 14, 7, 0)]

    async def test_with_nothing_to_move_a_past_hour_is_not_what_is_said(
        self, keeper: Keeper
    ) -> None:
        """There is no alarm to move, and that comes before the hour being one that has gone by."""
        refused = await keeper.move(7, 0, AlarmDay(in_days=0))

        assert refused.reason == "none"

    async def test_the_ring_that_is_being_said_is_cut(self, keeper: Keeper) -> None:
        keeper.sayer.hold = asyncio.Event()
        await keeper.alarm(22, 5)
        await keeper.clock.advance(5 * 60)
        await asyncio.wait_for(keeper.sayer.saying.wait(), 2)

        await keeper.move(6, 0)
        await settle()

        assert keeper.sayer.cut == 1

    async def test_the_device_shows_that_it_was_moved(self, keeper: Keeper) -> None:
        await keeper.alarm(7, 0)

        await keeper.move(8, 30)

        assert keeper.shown.of("event")[-1] == "alarm a1 moved to 2026-10-14 08:30"


class TestStoppingWhatRings:
    async def test_a_timer_and_an_alarm_that_ring_are_stopped_together(
        self, keeper: Keeper
    ) -> None:
        await keeper.timer(60)
        await keeper.alarm(22, 1)
        await keeper.clock.advance(60)

        stopped = await keeper.stop()

        assert stopped.now == civil(2026, 10, 13, 22, 1)  # what the wall shows when it did
        assert [(timer.id, timer.ringing) for timer in stopped.timers] == [("t1", True)]
        assert [(alarm.id, alarm.ringing) for alarm in stopped.alarms] == [("a2", True)]
        assert keeper.state().timers == ()
        assert keeper.state().alarms == ()

    async def test_what_has_not_rung_is_left_alone(self, keeper: Keeper) -> None:
        await keeper.timer(600)
        await keeper.timer(60)
        await keeper.alarm(23, 0)
        await keeper.clock.advance(60)

        stopped = await keeper.stop()

        assert [timer.seconds for timer in stopped.timers] == [60]
        assert stopped.alarms == ()
        assert [timer.seconds for timer in keeper.state().timers] == [600]
        assert len(keeper.state().alarms) == 1

    async def test_with_nothing_ringing_nothing_is_stopped_and_nothing_is_cancelled(
        self, keeper: Keeper
    ) -> None:
        await keeper.timer(60)
        await keeper.alarm(7, 0)

        stopped = await keeper.stop()

        assert (stopped.timers, stopped.alarms) == ((), ())
        assert len(keeper.state().timers) == 1
        assert len(keeper.state().alarms) == 1
        assert (await Keeper().stop()) == RingsStopped(now=EVENING)

    async def test_it_rings_no_more(self, keeper: Keeper) -> None:
        await keeper.timer(60)
        await keeper.clock.advance(60)
        said = len(keeper.sayer.said)

        await keeper.stop()
        await keeper.clock.advance(RING_EVERY_S * 4)

        assert len(keeper.sayer.said) == said
        assert keeper.clock.sleepers == 0

    @pytest.mark.parametrize("what", ["a timer", "an alarm"])
    async def test_the_ring_that_is_being_said_is_cut(self, keeper: Keeper, what: str) -> None:
        keeper.sayer.hold = asyncio.Event()
        if what == "a timer":
            await keeper.timer(60)
        else:
            await keeper.alarm(22, 1)
        await keeper.clock.advance(60)
        await asyncio.wait_for(keeper.sayer.saying.wait(), 2)

        await keeper.stop()
        await settle()

        assert keeper.sayer.cut == 1

    async def test_the_device_shows_what_it_stopped(self, keeper: Keeper) -> None:
        await keeper.timer(60)
        await keeper.alarm(22, 1)
        await keeper.clock.advance(60)

        await keeper.stop()

        assert keeper.shown.of("event")[-2:] == ["timer t1 stopped", "alarm a2 stopped"]


class TestTimersAndAlarmsTogether:
    async def test_they_are_two_lists_and_one_does_not_cancel_the_other(
        self, keeper: Keeper
    ) -> None:
        await keeper.timer(60)
        await keeper.alarm(7, 0)

        await keeper.cancel_alarm()

        assert len(keeper.state().timers) == 1
        assert keeper.state().alarms == ()

    async def test_the_state_says_what_the_wall_shows(self, keeper: Keeper) -> None:
        await keeper.clock.advance(61)

        assert keeper.state().now == civil(2026, 10, 13, 22, 1).replace(second=1)

    async def test_a_device_with_nothing_set_says_so(self) -> None:
        keeper = Keeper()

        assert keeper.state() == TimeState(now=EVENING)
        assert keeper.clock.sleepers == 0
        await keeper.timekeeper.close()

    async def test_the_ids_are_not_given_twice(self, keeper: Keeper) -> None:
        await keeper.timer(60)
        await keeper.cancel_timer()
        await keeper.timer(60)
        await keeper.alarm(7, 0)

        assert [timer.id for timer in keeper.state().timers] == ["t2"]
        assert [alarm.id for alarm in keeper.state().alarms] == ["a3"]


class TestWhoSaysIt:
    async def test_with_no_client_it_is_said_on_the_speaker_of_the_device(self) -> None:
        said: list[AudioPayload] = []

        class Output:
            async def play(self, audio: AudioPayload, *, alert: bool = False) -> None:
                said.append(audio)

        clock = FakeClock(EVENING)
        keeper = Timekeeper(RecordingRenderer(), clock=clock, output=Output())
        await keeper.execute(SetTimer(seconds=30, text="Done.", audio=clip(3)))

        await clock.advance(30)

        assert said == [clip(3)]
        await keeper.close()

    async def test_on_the_speaker_of_the_device_a_timer_rings_as_an_alert_every_time(self) -> None:
        output, clock = FakeVoiceOutput(), FakeClock(EVENING)
        keeper = Timekeeper(RecordingRenderer(), clock=clock, output=output)
        await keeper.execute(SetTimer(seconds=30, text="Done.", audio=clip(3)))

        await clock.advance(30)
        await clock.advance(RING_EVERY_S + 2)  # nobody stopped it: it rings again

        assert output.played == [clip(3), clip(3)]
        assert output.alerts == [True, True]  # a mute does not silence a ring
        await keeper.close()

    async def test_on_the_speaker_of_the_device_an_alarm_rings_as_an_alert_too(self) -> None:
        output, clock = FakeVoiceOutput(), FakeClock(EVENING)
        keeper = Timekeeper(RecordingRenderer(), clock=clock, output=output)
        await keeper.execute(SetAlarm(hour=22, minute=1, text="Up.", audio=clip(4)))

        await clock.advance(61)

        assert output.played == [clip(4)]  # the clip that the alarm was set with
        assert output.alerts == [True]
        await keeper.close()

    async def test_a_client_that_is_connected_says_it_and_one_that_is_gone_does_not(self) -> None:
        said: list[AudioPayload] = []

        class Output:
            async def play(self, audio: AudioPayload, *, alert: bool = False) -> None:
                said.append(audio)

        shown, clock, sayer = RecordingRenderer(), FakeClock(EVENING), Sayer()
        keeper = Timekeeper(shown, clock=clock, output=Output())
        keeper.attach(sayer)
        await keeper.execute(SetTimer(seconds=30, text="Done.", audio=clip(3)))

        await clock.advance(30)
        assert len(sayer.said) == 1
        assert said == []

        keeper.detach(sayer)  # the connection is lost, and the timer is not
        await clock.advance(RING_EVERY_S)
        assert len(sayer.said) == 1
        assert said == [clip(3)]
        await keeper.close()

    async def test_a_client_that_was_replaced_is_not_detached_by_being_left_late(self) -> None:
        shown, clock = RecordingRenderer(), FakeClock(EVENING)
        keeper = Timekeeper(shown, clock=clock)
        old, new = Sayer(), Sayer()
        keeper.attach(old)
        keeper.attach(new)  # the next connection came before the last one was let go of

        keeper.detach(old)
        await keeper.execute(SetTimer(seconds=30, text="Done.", audio=clip()))
        await clock.advance(30)

        assert len(new.said) == 1
        await keeper.close()

    async def test_with_nobody_to_say_it_the_words_are_shown(self) -> None:
        shown, clock = RecordingRenderer(), FakeClock(EVENING)
        keeper = Timekeeper(shown, clock=clock)
        await keeper.execute(SetTimer(seconds=30, text="Tea is ready.", audio=clip()))

        await clock.advance(30)

        assert shown.of("speech") == ["Tea is ready."]
        await keeper.close()

    @pytest.mark.parametrize(
        "failure",
        [OSError("the speaker is unplugged"), RuntimeError("the sound card went"), ValueError("x")],
        ids=lambda failure: type(failure).__name__,
    )
    async def test_a_speaker_that_fails_in_any_way_is_tried_again_at_the_next_ring(
        self, failure: Exception
    ) -> None:
        shown, clock = RecordingRenderer(), FakeClock(EVENING)

        class Failing:
            def __init__(self) -> None:
                self.calls = 0

            async def play(self, audio: AudioPayload, *, alert: bool = False) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise failure

        output = Failing()
        keeper = Timekeeper(shown, clock=clock, output=output)
        await keeper.execute(SetTimer(seconds=30, text="Done.", audio=clip()))

        await clock.advance(30)
        assert shown.of("notice") == [f"a ring could not be said: {failure}"]
        await clock.advance(RING_EVERY_S)

        assert output.calls == 2
        await keeper.close()


class TestWhatStoppingTheProgramLoses:
    async def test_closing_it_forgets_everything_and_rings_nothing(self, keeper: Keeper) -> None:
        keeper.sayer.hold = asyncio.Event()
        await keeper.timer(60)
        await keeper.alarm(23, 0)
        await keeper.clock.advance(60)
        await asyncio.wait_for(keeper.sayer.saying.wait(), 2)

        await keeper.timekeeper.close()

        assert keeper.state().timers == ()
        assert keeper.state().alarms == ()
        assert keeper.sayer.cut == 1
        said = len(keeper.sayer.said)
        await keeper.clock.advance(3600)
        assert len(keeper.sayer.said) == said

    async def test_closing_a_keeper_that_keeps_nothing_is_nothing(self) -> None:
        keeper = Keeper()

        await keeper.timekeeper.close()
        await keeper.timekeeper.close()


class TestTheClockOfTheMachine:
    def test_the_wall_is_civil_time_to_the_second(self) -> None:
        wall = SystemClock().wall()

        assert wall.tzinfo is None
        assert wall.microsecond == 0

    def test_the_other_clock_only_goes_forward(self) -> None:
        clock = SystemClock()

        assert clock.monotonic() <= clock.monotonic()

    async def test_it_sleeps(self) -> None:
        await SystemClock().sleep(0)
