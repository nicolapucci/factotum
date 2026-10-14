"""What the series judges of a timer and an alarm (ADR 0029): the device that was spoken to kept the
time, and what it kept is what was asked for.

A timer or an alarm is not a call to a service or a command that is over when it is sent: it is
something that the device *holds*, on a clock of its own, so what a case is judged by is what the
device held before the message and what it holds after. The clocks of the devices of the series do
not move by themselves (``Device.clock``), so a timer is as long as it was set for and an alarm is
at the moment it was set to, and what is said of them is what the whole assistant made of the
answer of the device.

The judge counts what the device did on its own (the same functions that the unit tests of the
device are tested with: ``next_moment``, ``parse_duration``) and the sentences it expects are those
of ``factotum.actions.core.phrases``, which a unit test of their own holds to what they say.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from factotum.actions.core.phrases import (
    CoreSay,
    core_say,
    date_sentence,
    say_clock,
    say_duration,
    say_when,
    time_sentence,
)
from factotum.actions.core.when import AlarmWords, parse_alarm, parse_duration
from factotum.actions.plan_cases import PlanCase
from factotum.client_core.moments import next_moment
from factotum.client_core.timekeeping import DEFAULT_SNOOZE_S
from factotum.contracts.timekeeping import AlarmInfo, TimerInfo, TimeState
from factotum.e2e.models import is_italian

if TYPE_CHECKING:
    from factotum.e2e.series import Window


def judge(case: PlanCase, window: "Window") -> list[str]:
    """What is wrong with what came of a message about a timer or an alarm."""
    target = case.device or case.speaker
    language = "it" if is_italian(case.message) else "en"
    before, after = window.time_before[target], window.time[target]
    reply = window.outcome.reply if window.outcome is not None and window.outcome.reply else ""
    problems: list[str] = []
    match case.skill:
        case "timer":
            problems += _timer_set(case, language, before, after, window.events[target], reply)
        case "timer-cancel":
            problems += _timer_cancelled(
                case, language, before, after, window.events[target], reply
            )
        case "timer-status":
            problems += _timer_said(language, before, after, window.events[target], reply)
        case "alarm":
            problems += _alarm_set(case, language, before, after, window.events[target], reply)
        case "alarm-cancel":
            problems += _alarm_cancelled(
                case, language, before, after, window.events[target], reply
            )
        case "alarm-postpone":
            problems += _alarm_postponed(
                case, language, before, after, window.events[target], reply
            )
        case "alarm-change":
            problems += _alarm_moved(case, language, before, after, window.events[target], reply)
        case "stop":
            problems += _rings_stopped(language, before, after, window.events[target], reply)
        case "alarm-status":
            problems += _alarm_said(language, before, after, window.events[target], reply)
        case "clock":
            problems += _clock_said(case, language, before, after, window.events[target], reply)
    for name, was in window.time_before.items():
        if name != target and window.time[name] != was:
            problems.append(f"{name} had its timers or its alarms changed")
    if window.jobs:  # a skill of the platform is code of the actions service: no sandbox runs it
        problems.append(f"it ran {[job.label for job in window.jobs]}")
    if window.web:
        problems.append(f"it reached {[request.target for request in window.web]}")
    if window.spoken is not None and window.spoken[case.speaker] != (reply,):
        problems.append(f"{case.speaker} said {window.spoken[case.speaker]} and not {reply!r}")
    return problems


def _new_timers(before: TimeState, after: TimeState) -> list[TimerInfo]:
    kept = {timer.id for timer in before.timers}
    return [timer for timer in after.timers if timer.id not in kept]


def _new_alarms(before: TimeState, after: TimeState) -> list[AlarmInfo]:
    kept = {alarm.id for alarm in before.alarms}
    return [alarm for alarm in after.alarms if alarm.id not in kept]


def _order(id_: str) -> int:
    return int(id_[1:])


def _timer_set(
    case: PlanCase,
    language: str,
    before: TimeState,
    after: TimeState,
    events: tuple[str, ...],
    reply: str,
) -> list[str]:
    seconds = parse_duration(str(case.params["duration"]))
    new = _new_timers(before, after)
    problems: list[str] = []
    if len(new) != 1 or len(after.timers) != len(before.timers) + 1:
        return [f"it has {len(after.timers)} timers, and had {len(before.timers)}"]
    [timer] = new
    if timer.seconds != seconds or timer.left_s != seconds:
        problems.append(f"the timer is for {timer.seconds} s ({timer.left_s} left), not {seconds}")
    if events != (f"timer {timer.id} set for {timer.seconds} s",):
        problems.append(f"it was told {events}")
    wanted = core_say(language, CoreSay.TIMER_SET, duration=say_duration(language, timer.seconds))
    if reply != wanted:
        problems.append(f"it was said {reply!r} instead of {wanted!r}")
    if after.alarms != before.alarms:
        problems.append("an alarm was touched")
    return problems


def _timer_cancelled(
    case: PlanCase,
    language: str,
    before: TimeState,
    after: TimeState,
    events: tuple[str, ...],
    reply: str,
) -> list[str]:
    named = case.params.get("duration")
    seconds = parse_duration(str(named)) if named is not None else None
    held = sorted(before.timers, key=lambda timer: (timer.left_s, _order(timer.id)))
    fitting = [timer for timer in held if seconds in (None, timer.seconds)]
    chosen = [timer for timer in fitting if timer.ringing] or fitting[:1]
    problems: list[str] = []
    if {timer.id for timer in after.timers} != {t.id for t in before.timers} - {
        t.id for t in chosen
    }:
        problems.append(
            f"it holds {[t.id for t in after.timers]} after cancelling {[t.id for t in chosen]}"
        )
    if events != tuple(f"timer {timer.id} cancelled" for timer in chosen):
        problems.append(f"it was told {events}")
    if not chosen:
        key = CoreSay.TIMER_NONE if seconds is None else CoreSay.TIMER_NONE_NAMED
        duration = say_duration(language, seconds or 1)
        wanted = core_say(language, key, duration=duration)
    elif len(chosen) > 1:
        wanted = core_say(language, CoreSay.TIMERS_CANCELLED, count=len(chosen))
    else:
        duration = say_duration(language, chosen[0].seconds)
        wanted = core_say(language, CoreSay.TIMER_CANCELLED, duration=duration)
    if reply != wanted:
        problems.append(f"it was said {reply!r} instead of {wanted!r}")
    return problems


def _timer_said(
    language: str, before: TimeState, after: TimeState, events: tuple[str, ...], reply: str
) -> list[str]:
    problems: list[str] = []
    if after != before or events:
        problems.append(f"a question changed something: {events}")
    timers = sorted(before.timers, key=lambda timer: (timer.left_s, _order(timer.id)))
    if not timers:
        wanted = [core_say(language, CoreSay.TIMERS_NONE)]
    else:
        wanted = [say_duration(language, timer.left_s) for timer in timers]
    problems.extend(f"{said!r} is not in {reply!r}" for said in wanted if said not in reply)
    if len(timers) > 1 and str(len(timers)) not in reply:
        problems.append(f"the number of timers ({len(timers)}) is not in {reply!r}")
    return problems


def _alarm_set(
    case: PlanCase,
    language: str,
    before: TimeState,
    after: TimeState,
    events: tuple[str, ...],
    reply: str,
) -> list[str]:
    words = parse_alarm(f"{case.params.get('day', '')} {case.params['time']}")
    new = _new_alarms(before, after)
    if words is None or len(new) != 1 or len(after.alarms) != len(before.alarms) + 1:
        return [f"it has {len(after.alarms)} alarms, and had {len(before.alarms)}"]
    [alarm] = new
    moment = next_moment(before.now, words.hour, words.minute, words.day)
    problems: list[str] = []
    if alarm.at != moment:
        problems.append(f"the alarm is for {alarm.at} and not {moment}")
    if events != (f"alarm {alarm.id} set for {alarm.at:%Y-%m-%d %H:%M}",):
        problems.append(f"it was told {events}")
    when = say_when(language, alarm.at, before.now)
    wanted = core_say(language, CoreSay.ALARM_SET, when=when)
    if reply != wanted:
        problems.append(f"it was said {reply!r} instead of {wanted!r}")
    if after.timers != before.timers:
        problems.append("a timer was touched")
    return problems


def _alarm_cancelled(
    case: PlanCase,
    language: str,
    before: TimeState,
    after: TimeState,
    events: tuple[str, ...],
    reply: str,
) -> list[str]:
    named = case.params.get("time")
    words = parse_alarm(str(named)) if named is not None else None
    held = sorted(before.alarms, key=lambda alarm: (alarm.at, _order(alarm.id)))
    fitting = [
        alarm
        for alarm in held
        if words is None or (alarm.at.hour, alarm.at.minute) == (words.hour, words.minute)
    ]
    chosen = [alarm for alarm in fitting if alarm.ringing] or fitting[:1]
    problems: list[str] = []
    if {alarm.id for alarm in after.alarms} != {a.id for a in before.alarms} - {
        a.id for a in chosen
    }:
        problems.append(
            f"it holds {[a.id for a in after.alarms]} after cancelling {[a.id for a in chosen]}"
        )
    if events != tuple(f"alarm {alarm.id} cancelled" for alarm in chosen):
        problems.append(f"it was told {events}")
    if not chosen:
        if words is None:
            wanted = core_say(language, CoreSay.ALARM_NONE)
        else:
            at = say_clock(language, words.hour, words.minute)
            wanted = core_say(language, CoreSay.ALARM_NONE_NAMED, at=at)
    elif len(chosen) > 1:
        wanted = core_say(language, CoreSay.ALARMS_CANCELLED, count=len(chosen))
    else:
        when = say_when(language, chosen[0].at, before.now)
        wanted = core_say(language, CoreSay.ALARM_CANCELLED, when=when)
    if reply != wanted:
        problems.append(f"it was said {reply!r} instead of {wanted!r}")
    return problems


def _alarm_said(
    language: str, before: TimeState, after: TimeState, events: tuple[str, ...], reply: str
) -> list[str]:
    problems: list[str] = []
    if after != before or events:
        problems.append(f"a question changed something: {events}")
    alarms = sorted(before.alarms, key=lambda alarm: (alarm.at, _order(alarm.id)))
    if not alarms:
        wanted = [core_say(language, CoreSay.ALARMS_NONE)]
    else:
        wanted = [say_when(language, alarm.at, before.now) for alarm in alarms]
    problems.extend(f"{said!r} is not in {reply!r}" for said in wanted if said not in reply)
    if len(alarms) > 1 and str(len(alarms)) not in reply:
        problems.append(f"the number of alarms ({len(alarms)}) is not in {reply!r}")
    return problems


def _clock_said(
    case: PlanCase,
    language: str,
    before: TimeState,
    after: TimeState,
    events: tuple[str, ...],
    reply: str,
) -> list[str]:
    """The wall of the device, said as it shows it: the time, the day, or both when nobody said
    which. A question changes nothing."""
    problems: list[str] = []
    if after != before or events:
        problems.append(f"a question changed something: {events}")
    about = case.params.get("about")
    sentences = []
    if about != "date":
        sentences.append(time_sentence(language, before.now))
    if about != "time":
        sentences.append(date_sentence(language, before.now))
    wanted = " ".join(sentences)
    if reply != wanted:
        problems.append(f"it was said {reply!r} instead of {wanted!r}")
    return problems


def _chosen_alarms(before: TimeState, named: AlarmWords | None) -> list[AlarmInfo]:
    """The alarm that a device takes for the one meant: the one named by its hour, else the ones
    that ring, else the next to ring."""
    held = sorted(before.alarms, key=lambda alarm: (alarm.at, _order(alarm.id)))
    fitting = [
        alarm
        for alarm in held
        if named is None or (alarm.at.hour, alarm.at.minute) == (named.hour, named.minute)
    ]
    return [alarm for alarm in fitting if alarm.ringing] or fitting[:1]


def _alarms_changed(
    language: str,
    before: TimeState,
    after: TimeState,
    events: tuple[str, ...],
    reply: str,
    *,
    chosen: list[AlarmInfo],
    moments: dict[str, datetime],
    told: str,
    many: CoreSay,
    nothing: str,
) -> list[str]:
    """Alarms that were put off or moved: each of the chosen is where ``moments`` says and quiet,
    the others are as they were, the timers are untouched, the device was told once for each of
    the chosen, and the person was told where it is (or that there was none)."""
    problems: list[str] = []
    wanted = {
        alarm.id: AlarmInfo(
            id=alarm.id,
            at=moments.get(alarm.id, alarm.at),
            ringing=False if alarm.id in moments else alarm.ringing,
        )
        for alarm in before.alarms
    }
    if {alarm.id: alarm for alarm in after.alarms} != wanted:
        problems.append(f"it holds {list(after.alarms)} and not {list(wanted.values())}")
    if after.timers != before.timers:
        problems.append("a timer was touched")
    expected_events = tuple(
        f"alarm {alarm.id} {told} {moments[alarm.id]:%Y-%m-%d %H:%M}" for alarm in chosen
    )
    if events != expected_events:
        problems.append(f"it was told {events} and not {expected_events}")
    if not chosen:
        said = nothing
    elif len(chosen) > 1:
        said = core_say(language, many, count=len(chosen))
    else:
        when = say_when(language, moments[chosen[0].id], before.now)
        said = core_say(language, CoreSay.ALARM_WILL_RING, when=when)
    if reply != said:
        problems.append(f"it was said {reply!r} instead of {said!r}")
    return problems


def _alarm_postponed(
    case: PlanCase,
    language: str,
    before: TimeState,
    after: TimeState,
    events: tuple[str, ...],
    reply: str,
) -> list[str]:
    """An alarm put off: by as long as was said, or by the snooze of the device when nothing was;
    from now when it was ringing (it is silenced and rings again), from its own moment when it
    was not."""
    named = case.params.get("by")
    seconds = parse_duration(str(named)) if named is not None else DEFAULT_SNOOZE_S
    if seconds is None:
        return [f"the case has a length that is not one: {named!r}"]
    chosen = _chosen_alarms(before, None)
    delay = timedelta(seconds=seconds)
    moments = {alarm.id: (before.now if alarm.ringing else alarm.at) + delay for alarm in chosen}
    return _alarms_changed(
        language,
        before,
        after,
        events,
        reply,
        chosen=chosen,
        moments=moments,
        told="put off to",
        many=CoreSay.ALARMS_POSTPONED,
        nothing=core_say(language, CoreSay.ALARM_NONE_TO_CHANGE),
    )


def _alarm_moved(
    case: PlanCase,
    language: str,
    before: TimeState,
    after: TimeState,
    events: tuple[str, ...],
    reply: str,
) -> list[str]:
    """An alarm moved to the next time the new hour comes, on the day that was said."""
    words = parse_alarm(f"{case.params.get('day', '')} {case.params['time']}")
    named = parse_alarm(str(case.params["from"])) if "from" in case.params else None
    if words is None or ("from" in case.params and named is None):
        return ["the case has an hour that is not one"]
    moment = next_moment(before.now, words.hour, words.minute, words.day)
    if isinstance(moment, str):
        return [f"the case asks for a moment that cannot be ({moment})"]
    chosen = _chosen_alarms(before, named)
    if named is None:
        nothing = core_say(language, CoreSay.ALARM_NONE_TO_CHANGE)
    else:
        nothing = core_say(
            language, CoreSay.ALARM_NONE_NAMED, at=say_clock(language, named.hour, named.minute)
        )
    return _alarms_changed(
        language,
        before,
        after,
        events,
        reply,
        chosen=chosen,
        moments={alarm.id: moment for alarm in chosen},
        told="moved to",
        many=CoreSay.ALARMS_MOVED,
        nothing=nothing,
    )


def _rings_stopped(
    language: str, before: TimeState, after: TimeState, events: tuple[str, ...], reply: str
) -> list[str]:
    """What rings is stopped and what does not is left alone. The clocks of the series do not move,
    so nothing rings and the answer is that: but a device that held a ring would have it stopped."""
    ringing_timers = {timer.id for timer in before.timers if timer.ringing}
    ringing_alarms = {alarm.id for alarm in before.alarms if alarm.ringing}
    problems: list[str] = []
    if {timer.id for timer in after.timers} != {t.id for t in before.timers} - ringing_timers:
        problems.append(f"it holds the timers {[t.id for t in after.timers]} after stopping")
    if {alarm.id for alarm in after.alarms} != {a.id for a in before.alarms} - ringing_alarms:
        problems.append(f"it holds the alarms {[a.id for a in after.alarms]} after stopping")
    expected = sorted(
        [*(f"timer {each} stopped" for each in ringing_timers)]
        + [f"alarm {each} stopped" for each in ringing_alarms]
    )
    if sorted(events) != expected:
        problems.append(f"it was told {events} and not {expected}")
    wanted = core_say(
        language,
        CoreSay.RINGS_STOPPED if ringing_timers or ringing_alarms else CoreSay.NOTHING_RINGS,
    )
    if reply != wanted:
        problems.append(f"it was said {reply!r} instead of {wanted!r}")
    return problems
