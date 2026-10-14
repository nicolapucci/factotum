from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from factotum.contracts.commands import MAX_RESULT_DATA_CHARS, CommandResult
from factotum.contracts.timekeeping import (
    MAX_ALARMS,
    MAX_TIMERS,
    AlarmInfo,
    AlarmsCancelled,
    AlarmSet,
    AlarmsMoved,
    AlarmsPostponed,
    RingsStopped,
    TimerInfo,
    TimersCancelled,
    TimerSet,
    TimeState,
    answer_in,
    data_of,
)

NOW = datetime(2026, 10, 13, 22, 1, 14)  # noqa: DTZ001 - civil time: the wall has no zone
TOMORROW = datetime(2026, 10, 14, 7, 0)  # noqa: DTZ001


def a_timer(index: int = 1, /, **changes: object) -> TimerInfo:
    fields: dict[str, object] = {"id": f"t{index}", "seconds": 300, "left_s": 274}
    return TimerInfo.model_validate({**fields, **changes})


def an_alarm(index: int = 1, /, **changes: object) -> AlarmInfo:
    fields: dict[str, object] = {"id": f"a{index}", "at": TOMORROW}
    return AlarmInfo.model_validate({**fields, **changes})


class TestWhatAMomentOnTheWallIs:
    def test_it_has_no_zone_because_the_wall_of_a_device_shows_none(self) -> None:
        zoned = datetime(2026, 10, 14, 7, 0, tzinfo=UTC)

        with pytest.raises(ValidationError, match="no zone"):
            AlarmInfo(id="a1", at=zoned)
        with pytest.raises(ValidationError, match="no zone"):
            TimeState(now=zoned)
        with pytest.raises(ValidationError, match="no zone"):
            AlarmSet(now=zoned, reason="past")
        with pytest.raises(ValidationError, match="no zone"):
            AlarmsCancelled(now=zoned, remaining=0)
        with pytest.raises(ValidationError, match="no zone"):
            AlarmsPostponed(now=zoned)
        with pytest.raises(ValidationError, match="no zone"):
            AlarmsMoved(now=zoned, reason="past")
        with pytest.raises(ValidationError, match="no zone"):
            RingsStopped(now=zoned)

    def test_it_travels_as_the_time_that_the_wall_shows(self) -> None:
        said = data_of(AlarmSet(now=NOW, alarm=an_alarm()))

        assert said["now"] == "2026-10-13T22:01:14"
        assert said["alarm"] == {"id": "a1", "at": "2026-10-14T07:00:00", "ringing": False}


class TestWhatADeviceSaysOfItsTime:
    def test_it_is_read_out_of_an_answer_that_has_the_volume_in_it_as_well(self) -> None:
        answer = {
            "volume": 40,
            "muted": False,
            "now": "2026-10-13T22:01:14",
            "timers": [{"id": "t1", "seconds": 300, "left_s": 274, "ringing": False}],
            "alarms": [{"id": "a1", "at": "2026-10-14T07:00:00", "ringing": False}],
        }

        state = answer_in(TimeState, answer)

        assert state == TimeState(now=NOW, timers=(a_timer(),), alarms=(an_alarm(),))

    def test_a_device_with_nothing_set_says_so(self) -> None:
        state = answer_in(TimeState, {"now": "2026-10-13T22:01:14", "timers": [], "alarms": []})

        assert state is not None
        assert state.timers == ()
        assert state.alarms == ()

    @pytest.mark.parametrize(
        "answer",
        [
            None,
            {},
            {"volume": 40, "muted": False},
            {"now": "yesterday"},
            {"now": "2026-10-13T22:01:14", "timers": "none"},
            {"now": "2026-10-13T22:01:14", "timers": [{"id": "t1"}]},
            {"now": "2026-10-13T22:01:14", "alarms": [{"id": "a1", "at": "tomorrow"}]},
            {"now": "2026-10-13T22:01:14+02:00"},
        ],
    )
    def test_an_answer_that_is_not_one_is_nothing(self, answer: dict[str, object] | None) -> None:
        """A device that answers what a skill cannot read did not answer: nothing is guessed."""
        assert answer_in(TimeState, answer) is None

    def test_a_timer_has_a_length_and_what_is_left_of_it(self) -> None:
        wrong: list[dict[str, object]] = [
            {"seconds": 0},
            {"left_s": -1},
            {"id": ""},
            {"id": "x" * 33},
        ]
        for changes in wrong:
            with pytest.raises(ValidationError):
                a_timer(**changes)
        assert a_timer(left_s=0, ringing=True).ringing is True

    def test_the_most_that_a_device_holds_fits_in_the_answer_it_may_give(self) -> None:
        state = TimeState(
            now=NOW,
            timers=tuple(a_timer(index, id=f"timer-{index:020d}") for index in range(MAX_TIMERS)),
            alarms=tuple(an_alarm(index, id=f"alarm-{index:020d}") for index in range(MAX_ALARMS)),
        )

        answered = CommandResult.ok({"volume": 100, "muted": False, **data_of(state)})

        assert answered.data is not None
        assert len(answered.model_dump_json()) < MAX_RESULT_DATA_CHARS


class TestWhatADeviceSaysOfWhatItDid:
    def test_a_timer_that_was_set(self) -> None:
        said = data_of(TimerSet(timer=a_timer()))

        assert answer_in(TimerSet, said) == TimerSet(timer=a_timer())

    def test_a_timer_that_was_not_set_says_why(self) -> None:
        refused = answer_in(TimerSet, {"reason": "too_many"})

        assert refused == TimerSet(reason="too_many")
        assert refused is not None
        assert refused.timer is None
        assert answer_in(TimerSet, {"reason": "because"}) is None

    def test_an_answer_that_sets_nothing_and_says_nothing_is_not_an_answer(self) -> None:
        both = {"timer": data_of(a_timer()), "reason": "too_many"}
        alarm_and_reason = {
            "now": "2026-10-13T22:01:14",
            "alarm": data_of(an_alarm()),
            "reason": "past",
        }

        assert answer_in(TimerSet, {}) is None
        assert answer_in(TimerSet, both) is None
        assert answer_in(AlarmSet, {"now": "2026-10-13T22:01:14"}) is None
        assert answer_in(AlarmSet, alarm_and_reason) is None

    def test_the_timers_that_were_stopped_and_how_many_are_left(self) -> None:
        said = data_of(TimersCancelled(cancelled=(a_timer(),), remaining=2))

        assert answer_in(TimersCancelled, said) == TimersCancelled(
            cancelled=(a_timer(),), remaining=2
        )
        assert answer_in(TimersCancelled, {"cancelled": []}) is None  # how many are left is said

    def test_an_alarm_that_was_set_is_at_the_moment_the_device_chose(self) -> None:
        said = answer_in(AlarmSet, data_of(AlarmSet(now=NOW, alarm=an_alarm())))

        assert said is not None
        assert said.alarm is not None
        assert said.alarm.at == TOMORROW
        assert said.now == NOW

    @pytest.mark.parametrize("reason", ["past", "no_such_day", "too_many"])
    def test_an_alarm_that_was_not_set_says_why(self, reason: str) -> None:
        said = answer_in(AlarmSet, {"now": "2026-10-13T22:01:14", "reason": reason})

        assert said is not None
        assert said.alarm is None
        assert said.reason == reason

    def test_the_alarms_that_were_stopped(self) -> None:
        said = answer_in(
            AlarmsCancelled,
            data_of(AlarmsCancelled(now=NOW, cancelled=(an_alarm(),), remaining=0)),
        )

        assert said is not None
        assert said.cancelled == (an_alarm(),)

    def test_the_alarms_that_were_put_off_are_as_they_are_now(self) -> None:
        later = an_alarm(at=datetime(2026, 10, 14, 7, 10))  # noqa: DTZ001 - civil time
        said = answer_in(AlarmsPostponed, data_of(AlarmsPostponed(now=NOW, postponed=(later,))))

        assert said is not None
        assert said.postponed == (later,)
        assert said.now == NOW

    def test_none_put_off_is_an_answer_too(self) -> None:
        said = answer_in(AlarmsPostponed, {"now": "2026-10-13T22:01:14"})

        assert said == AlarmsPostponed(now=NOW)
        assert answer_in(AlarmsPostponed, {"postponed": []}) is None  # what the wall shows is said

    def test_the_alarms_that_were_moved_are_as_they_are_now(self) -> None:
        said = answer_in(AlarmsMoved, data_of(AlarmsMoved(now=NOW, moved=(an_alarm(),))))

        assert said is not None
        assert said.moved == (an_alarm(),)
        assert said.reason is None

    @pytest.mark.parametrize("reason", ["none", "past", "no_such_day"])
    def test_an_alarm_that_was_not_moved_says_why(self, reason: str) -> None:
        said = answer_in(AlarmsMoved, {"now": "2026-10-13T22:01:14", "reason": reason})

        assert said is not None
        assert said.moved == ()
        assert said.reason == reason
        assert answer_in(AlarmsMoved, {"now": "2026-10-13T22:01:14", "reason": "because"}) is None

    def test_alarms_were_moved_or_it_says_why_none_was_and_never_both_or_neither(self) -> None:
        both = {"now": "2026-10-13T22:01:14", "moved": [data_of(an_alarm())], "reason": "past"}

        assert answer_in(AlarmsMoved, {"now": "2026-10-13T22:01:14"}) is None
        assert answer_in(AlarmsMoved, both) is None

    def test_what_was_stopped_is_the_timers_and_the_alarms_that_rang(self) -> None:
        ringing_timer = a_timer(left_s=0, ringing=True)
        ringing_alarm = an_alarm(ringing=True)
        stopped = RingsStopped(now=NOW, timers=(ringing_timer,), alarms=(ringing_alarm,))

        assert answer_in(RingsStopped, data_of(stopped)) == stopped

    def test_nothing_stopped_is_an_answer_too_but_no_answer_is_not_that(self) -> None:
        assert answer_in(RingsStopped, {"now": "2026-10-13T22:01:14"}) == RingsStopped(now=NOW)
        assert answer_in(RingsStopped, {}) is None  # a device that said nothing did not say it
        assert answer_in(RingsStopped, None) is None

    @given(st.dictionaries(st.text(max_size=8), st.integers() | st.text(max_size=8), max_size=4))
    def test_what_is_not_an_answer_is_never_an_error_for_the_skill_that_reads_it(
        self, rubbish: dict[str, object]
    ) -> None:
        shapes = (
            TimeState,
            TimerSet,
            TimersCancelled,
            AlarmSet,
            AlarmsCancelled,
            AlarmsPostponed,
            AlarmsMoved,
            RingsStopped,
        )
        for shape in shapes:
            answer_in(shape, rubbish)
