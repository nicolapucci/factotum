# 0029. The skills for what a device does are the interface between the model and the device

Status: accepted, 2026-10-13. Built: the sound (volume, mute, how it is). Not built yet: timers and alarms.

## Context

[ADR 0028](0028-the-model-alone-does-nothing-a-request-is-satisfied-by-skills-or-declined.md) made
every request a matter of skills, and left the volume as an example skill that a script ran in a
sandbox and that told the device a step of its own. The maintainer's note after it (2026-10-13) is
the rule for what comes next: a skill for a function that needs the client device to do something
(a timer, a change of the volume, ...) **is the interface between the model and the device**, and
the function lives on the device. For an alarm the device has its own clock and uses it: the skill
only tells the device to set the alarm for day *x* at hour *y*. And when a value was not said, the
default belongs to the device and not to the skill.

## Decision

- **The skill is the contract; the device is the function.** Toward the model the skill says what can
  be asked (a manifest, whose values are checked in code against the words of the person: ADR 0028).
  Toward the device it passes on what was said as a command of the vocabulary. The model never speaks
  to a device, never names a command and never learns what the device did. The skill has no logic of
  the device's own: no step, no sound, no time that nobody said.
- **These skills are code of the platform** (ADR 0026): they run in the actions service, with no
  sandbox and no runner, so they really do it (the example skills answer with a dry run when there is
  no runner). The example skills `volume` and `volume-change` are gone from `skills/examples`: they are
  the platform's now (a person's own skill of the same name would be added to it as a version, as
  ADR 0026 says).
- **A device answers, and the answer is for the skill.** The result of a command (`CommandResult`)
  may carry `data`, a small JSON (at most 4000 characters): the level that the volume came to, how the
  device is. It travels as the result already did (device, `ack`, edge, actions service). The skill
  of the platform that gave the command reads it **in code** and says it in a sentence of the code, in
  the language of the person (`CoreResult.after`); a model never reads what a service answered
  (ADR 0016), and a script is never given it. A device that does not answer what the skill needs is a
  skill that failed, and not a guess.
- **A question to a device is `get_state`**, and only the platform may ask it
  (`PLATFORM_COMMAND_MODELS`, with the clip of `play_audio`): a script or a plan cannot ask a device
  anything, because there is nobody it could tell. The answer lists what the device has (`volume` and
  `muted` for a speaker; the timers and alarms that it holds, later).
- **Mute keeps the level.** `set_mute` mutes the sound or brings it back where it was. Setting or
  turning the volume brings the sound back, as it does on a television.
- **Timers and alarms live on the device** (not built yet; this is what it will be):
  - *A timer*: `timer` takes the **words** of the duration as the person said them (*cinque minuti*,
    *un'ora e mezza*, *mezz'ora*, *2h30*), which the check of ADR 0028 can find in the message, and
    code turns them into seconds (it and en): a model does no sums, and "300" is a number that is
    not in the message. The skill has the voice of the tenant make the sentence that rings, as
    `announce` does, and gives the device `set_timer` with the clip; the device keeps the time on its
    own clock, rings when it is due (when nothing else is being said), and goes on until it is
    stopped. `timer-cancel` and `timer-status` (the latter asks the device and says its answer).
  - *An alarm*: `alarm` takes the words of the time and, if there is one, of the day (*alle sette e
    mezza*, *domani*, *lunedì*, *il 15 ottobre*), and code reads them into an hour and a minute and a
    day that is *a way of saying a day*, not a date: the platform has no clock and does not need one.
    The device, which has the clock and the time zone of the place it is in, resolves it to the next
    time that it comes, and answers with the moment that it chose so that the skill can say it. The
    skill says only: set the alarm for this day at this hour. `alarm-cancel` and `alarm-status` as for
    the timer.
  - *What a device holds is lost when its program stops* in the first version, and a repeating alarm
    (every morning) is not in it: both are for a decision of their own.

## Consequences

- A device that has the hands for a function implements it once, for every skill that may use it, and
  the skills stay thin. The same *turn it up* is a different number of points on a speaker and on a
  television, because the number is the device's.
- The vocabulary of commands is version 4 (`set_mute`, `get_state`, the `data` of a result), and a
  device that does not know a command says *unsupported* and the skill is told the truth.
- A planner that has more skills to choose from is asked about more messages that are near a skill
  (*what is the volume?* is a question in form and a command in effect); how well a small model tells
  them is measured with the corpus (`factotum.actions.plan_cases`) when a model is free to be asked.

## Alternatives considered

- **Keep the timer and the alarm in the platform**, which fires a command to the device when one is
  due. It needs a clock in the platform (ADR 0028 says nothing may treat the time as a special case),
  a scheduler that survives restarts, and a device that is connected at the moment; a device that
  rings by itself rings when its connection is down too.
- **A model that writes the seconds of a timer, or the date of an alarm.** It would make facts of its
  own (ADR 0028), and the check that a number is in the message would refuse *300* for *five minutes*.
- **The platform computes the date of an alarm and sends an absolute moment.** It would need the
  time zone of the device, which only the device knows, and would turn *tomorrow at seven* into a
  moment that is wrong when the device is somewhere else than the platform thinks.
- **Mute as a volume of 0.** Unmuting would not know what the volume was.

## What was built, and what it found

The first slice: `set_mute` and `get_state`, the `data` of a result, `CoreResult.after` and the
language that a core skill is given, the skills `volume`, `volume-change`, `mute` and `volume-status`
(`factotum.actions.core.volume`, with their sentences in `core/phrases.py`), the muted state of the
speaker of the device application and of the hardware of the terminal, and the corpus and the series
of `e2e` that say the new cases to the three devices and judge them. Nothing was measured with a
model: the machine's graphics card was in use, and the planner is not shown the new skills in its
examples until it can be measured (ADR 0028 found that every example moves a case or two).
