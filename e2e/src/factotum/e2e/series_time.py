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

from typing import TYPE_CHECKING

from factotum.actions.core.phrases import (
    CoreSay,
    core_say,
    say_clock,
    say_duration,
    say_when,
)
from factotum.actions.core.when import parse_alarm, parse_duration
from factotum.actions.plan_cases import PlanCase
from factotum.client_core.moments import next_moment
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
        case "alarm-status":
            problems += _alarm_said(language, before, after, window.events[target], reply)
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
