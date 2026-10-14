# 0029. The skills for what a device does are the interface between the model and the device

Status: accepted, 2026-10-13. Built: the sound (volume, mute, how it is), the timers and the alarms,
and what puts an alarm off or moves it and what stops whatever rings.

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
- **Timers and alarms live on the device.** The device keeps the time on a clock of its own and rings
  by itself, even when its connection is down at that moment; the skill is the interface to it.
  - *A timer*: `timer` takes the **words** of the duration as the person said them (*cinque minuti*,
    *un'ora e mezza*, *mezz'ora*, *2h30*, *half an hour*), which the check of ADR 0028 finds in the
    message, and code turns them into seconds (`core/when.py`, English and Italian, mixed if the
    person mixes them): a model does no sums, and "300" is a number that is not in the message. A
    length that is not understood is a skill that says so in words of its own (nothing is guessed),
    and a timer is from a second to a day. The skill has the voice of whom the device acts for make
    the sentence that rings, once, when the timer is set (`core/rings.py`), and gives the device
    `set_timer` with the clip. `timer-cancel` and `timer-status` (the latter asks the device with
    `get_state` and says its answer).
  - *An alarm*: `alarm` takes the words of the hour and, if there is one, of the day (*alle sette e
    mezza*, *domani*, *lunedì*, *il 15 ottobre*, *half past seven*, *7:30 pm*), and code reads them
    into an hour and a minute on the 24-hour clock (a period of the day, *di sera*, *pm*, makes the
    hour; with none it is as it was said) and a day that is **a way of saying a day** (`AlarmDay`:
    in some days, a weekday, a date), **not a date**: the platform has no clock and does not need
    one. The device, which has the clock and the calendar of the place it is in
    (`client_core/moments.py`), resolves it to the next time that it comes, and answers with the
    moment that it chose and the `now` it chose it from, so that the skill can say *domani alle 7*
    from the device's own today. A moment that has gone by (*today at six* at noon) or a date that
    never comes (the 31st of April) is not an alarm: the device answers why, and the skill says it.
    `alarm-cancel` and `alarm-status` as for the timer. A time that is counted from now (*in ten
    minutes*) is a timer and not an alarm, and is refused as one.
  - *Several at once*: a device holds up to eight timers and eight alarms. **A cancel** stops the
    one that the person named (a timer by its length, an alarm by its hour), else the ones that are
    ringing, else the next to ring: the choice is the device's, as the step of the volume is, and
    "cancel all" is not built (the skill is not given the words of the message, so nothing could
    check it).
  - *Ringing*: a device says the clip when nothing else is being said, as an announcement is said
    (it waits for an answer to end, and a person who speaks cuts it), and says it again every eight
    seconds until the timer or the alarm is cancelled, or for three minutes: a device in an empty
    house does not ring for ever. A timer is kept on the clock that only goes forward and an alarm
    on the wall (*civil time*, no zone), so that an alarm for seven rings at seven whatever the
    clock did in between. **A ring is an alert** (decided with the maintainer on 2026-10-13: *sveglie
    e timer rispettano il volume audio ma ignorano il muto*): the speaker plays it through a mute,
    since a ring that nobody can hear is no ring, and at the level the volume is at, as any sound.
    The ports of the sound say it (`alert` in `VoiceOutput.play` and `VoiceSink.begin`), and what
    a mute silences is still every other sound, an announcement included.
  - *What a device holds is lost when its program stops* in the first version (it is held in memory),
    and a repeating alarm (every morning) is not in it: both are for a decision of their own.
- **What lasts can be changed and stopped** (the maintainer, 2026-10-14). A skill that starts
  something that a device keeps going, in time or for as long as it is not stopped (a timer, an
  alarm, a ring, later music), comes with the skills that change it in time and the skills that stop
  it, and everything that is the device's own stays so: which one is meant when none is named, how
  long a snooze is when nobody says. For the alarm and for whatever rings that is:
  - *`alarm-postpone`* (*rimanda la sveglia*, *ancora cinque minuti*, *snooze*): the one that rings
    is silenced and rings again later, and one that has not rung is moved later. The person's words
    for how long are read by code, as for a timer; when there are none the device puts it off by
    its own snooze (`timekeeping.snooze_minutes` of the device application, 10), and the skill has
    none to tell. `postpone_alarm` carries `seconds` only when a person said it.
  - *`alarm-change`* (*sposta la sveglia alle otto*): the alarm is moved to the next time the new
    hour comes, on the day that was said (the same `AlarmDay`, the same reading by the device), in
    one command: a moment that has gone by, or a date that never comes, moves nothing and the old
    alarm stays, which a cancel followed by a new alarm could not promise. An alarm may be named by
    its hour (*dalle sette*), else the one that rings, else the next to ring (`move_alarm`).
  - *`stop`* (*basta*, *ferma*, *zitto*): everything that rings on the device that was spoken to,
    a timer or an alarm, is stopped, and nothing that has not rung is touched. A person who says
    *enough* does not say which it is, and *ferma* alone could be `timer-cancel` or `alarm-cancel`
    to a model, which answers *there is no timer* while an alarm rings. `stop_ringing` takes
    nothing. The cancels remain for a timer or an alarm that has not rung.
  - *The timer stays as it was* (set, cancel, status): the maintainer chose not to add a way to
    lengthen or pause it. *`media`* (pause, resume, stop for the commands that are already in the
    vocabulary) is not built either.

## Consequences

- A device that has the hands for a function implements it once, for every skill that may use it, and
  the skills stay thin. The same *turn it up* is a different number of points on a speaker and on a
  television, because the number is the device's.
- The vocabulary of commands is version 6 (`set_mute` and `get_state` with the `data` of a result,
  the timers and the alarms, and `postpone_alarm`, `move_alarm` and `stop_ringing`), and a device that
  does not know a command says *unsupported* and the skill is told the truth.
- Whoever adds a skill that starts something that lasts adds the means to change it and to stop it,
  or says in its record why not (the timer, on purpose; music, later).
- A planner that has more skills to choose from is asked about more messages that are near a skill
  (*what is the volume?* is a question in form and a command in effect); how well a small model tells
  them is measured with the corpus (`factotum.actions.plan_cases`): 135 of 143 commands with the right
  skill and 50 of 53 questions left as questions, with the 8B model and fifteen skills of the platform.

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

**The fourth slice**: the clock, and the first measurement of the skills of the platform with a real
model. `clock` (`core/clock.py`) says what time it is and what day by the wall of the device that was
spoken to. It asks the device how it is (`get_state`, whose answer already carried `TimeState.now`:
the vocabulary stays at version 6) and says what the wall shows in a sentence of `core/phrases.py`
(*Sono le 14 e 32*, *È l'una e 5*, *È mezzogiorno*, *It is 2:32 PM*, *Oggi è mercoledì 14 ottobre
2026*): the time, the day or both (`about`). The platform has no clock, so it works out nothing: a
device that does not answer its wall is a skill that failed. It tells the time of the place the device
is in and the date of today and nothing else; *che giorno è domani* needs a calendar, which only a
device has (an alarm asks it), and is not built. The corpus has 12 new cases and 7 new questions that
look like it, the series of `e2e` judges what is said from the wall the device had, and tests with a
clock that is set say it in both languages for each of two devices.

The skills of the platform (slices 1 to 4) were measured with a model for the first time, with
llama3.1 8B and `bge-m3` on the card, over the 196 messages of the corpus: **125 of 143 commands
exactly as meant, 135 with the right skill, 50 of 53 questions left as questions** (`clock` 12 of 12,
`stop` 8 of 8; the numbers and the list of what is wrong are in [`actions.md`](../actions.md#the-skills-of-the-platform-with-a-real-model-adr-0029-21-skills-196-messages)).
What it found:

* **The harness that measures a model could not give a perfect score.** It judged every skill by
  what the runner was asked, and the skills of the platform have no runner: 65 of the 76 commands it
  called wrong were its own mistake. It is `factotum.actions.measure` now, which reads what a skill
  was called with from the audit trail and which a unit test gives a planner that is told the
  right answer for the whole corpus, and asks for a perfect score.
* **A small model writes a value in the words it likes, and a text has to be made of the words of the
  message (ADR 0028).** *Sette e mezza* came back as `7:30` and `seven thirty`, *dieci minuti* as
  `ten minutes`, *domani* as `tomorrow`, *a ten minute timer* as `ten minutes`: good readings, left out
  by the check. With a required value the person was asked for what they had just said; with an
  optional one the skill applied its default and said nothing (*cancel the five minute timer* cancelled
  the next timer, *five more minutes* put the alarm off by ten). Telling the model to copy the words
  fixed most of the Italian alarms and moved the rest of the failures from one language to the other,
  depending on the language of the examples. **Open, and the maintainer's to decide:** a value that
  code reads (a length, an hour, a day: `core/when.py` reads both languages whichever is spoken) would
  count as said when the code reads it the way it reads something the person said, which keeps what the
  check is for (a made-up *300* for *five minutes* reads as nothing, and *10 minutes* for *ten*
  reads as the same length), and changes the sentence of ADR 0028 that says a text is made of the
  words of the message, for these values.
* **The reader did not know some ways of saying it** (*delle sette*, *dalle sette*, *dell'una*, *five
  more minutes*, *ancora cinque minuti*, *another hour*), and a model that copied the words exactly
  was a skill that failed. They are read now.
* **What a model is shown is fragile.** One example of a level (*Porta il volume a 35*) made three
  messages that were right read as a turn of the volume; a description that names the words of a skill
  (*louder, turn it up*) took a message of another skill. The answers are the same each time (the
  temperature is zero), so a few messages said once for each wording compare wordings in half a
  minute. A name does not steer (the skill that stops what rings was called `silence` for a run, and
  nothing changed). The examples of the planner for the platform's skills are made of their own text
  (`_of_the_platform`), so that what the model has been shown is what it is given.
* **A choice that the model is not given, it writes** (`both`, for the time and the day at once), and
  a value that is not one of the choices is a value that is missing.

Still wrong, and the model's: *stop the timer*, *ferma il timer* and *stop the alarm* go to `stop`
(which says that nothing is ringing when a timer has not rung) and not to the cancels; *avvisami tra
dieci minuti* is an alarm; *che giorno è domani?* is answered with today's date in Italian.

**The third slice**: what changes an alarm and what stops a ring. The vocabulary is version 6
(`postpone_alarm`, `move_alarm`, `stop_ringing`, all the platform's), the answers of the device are
`AlarmsPostponed`, `AlarmsMoved` and `RingsStopped` (`factotum.contracts.timekeeping`), the keeper
puts an alarm off, moves it and stops what rings (`Timekeeper`), the snooze of the device is a setting
of the device application (`timekeeping.snooze_minutes`), and the three skills are `alarm-postpone`
and `alarm-change` (`core/alarms.py`) and `stop` (`core/stop.py`). The corpus has 34 new cases (25
that ask, 9 questions that look like asking), the series of `e2e` judges them from what each device
held before and after (`series_time.py` works out which alarm the device should have taken, and
where it should have put it), and tests with a clock that moves ring an alarm, put it off, hear it
again ten minutes later, and stop a timer and an alarm that ring together. The same message from the
maintainer made a ring an alert: it goes through a mute.

What it found:

* **Nothing to change is a thing to say, not a failure.** *Rimanda la sveglia* with no alarm set is
  answered *Non c'è nessuna sveglia da cambiare*, as a cancel with nothing to cancel is. Only a moment
  that cannot be (gone by, a date that never comes) is a skill that failed, because the alarm that was
  asked for does not exist. It also keeps the corpus free of the order the cases are said in.
* **An answer made only of defaults is no answer.** `RingsStopped` with nothing in it is what a device
  that said nothing looks like, and it would have been read as *nothing was ringing*: it carries the
  time of the wall, so that an empty answer is not that.
* **Two rings that are up together are said one after the other, and the second waits for the next
  look at the clock** (at most a second): a test that moves time has to move it that second too.
* **The device decides which alarm is meant** (the named one, else the ones that ring, else the next
  to ring), and so does its snooze: the skill has neither, and a model that wrote *twenty minutes* for
  *snooze* gets none of it (ADR 0028).

It was measured with a model with the fourth slice.

**The second slice**: timers and alarms. The vocabulary is version 5 (`set_timer`, `cancel_timer`,
`set_alarm`, `cancel_alarm`), all four the platform's (`PLATFORM_COMMAND_MODELS`: a ring is a clip that
nobody else makes, and what a device says of its timers is for the skill that asked). What a device
answers is defined once, in `factotum.contracts.timekeeping` (`TimerSet`, `TimersCancelled`,
`AlarmSet`, `AlarmsCancelled`, and `TimeState` for the part of `get_state` that is about time), so that
the keeper that writes an answer and the skill that reads it cannot disagree. The keeper is
`factotum.client_core.Timekeeper`, with a `Clock` port (the machine's, or one that a test moves); the
six skills are in `core/timers.py` and `core/alarms.py`, the readers of what a person says in
`core/when.py`, and the sentences, with the words for a length, an hour and a day, in
`core/phrases.py`. The corpus has the new cases, the series of `e2e` says them to the three devices
and judges what each device *holds* before and after (`e2e/series_time.py`), and a test of its own
puts a whole day on a device by moving its clock.

What it found:

* **The keeper cannot live in a connection.** The first sketch gave the client its timers, and a client
  is made again at every reconnection (`stay_connected`), so a device that lost the edge for a minute
  lost its timer. The keeper is the device's and is lent to each client (`attach`): a connected
  client rings through its announcements (they wait for an answer, and are cut by a person who
  speaks), and a device with no client rings on the speaker itself.
* **A skill must be able to say why after the device answered.** *Today at six* is a question for the
  device's calendar, and it answers *past*: until now the only thing a skill could do after a device
  answered was say a sentence or fail with a generic one. `SkillFailedError` raised by `after` is now
  said in the skill's own words (*Quell'ora è già passata*), as it is when raised by `run`.
* **An answer that sets nothing is an answer.** A full device or a moment that has gone by answers
  `ok` with the reason, not `failed`: the command was understood and answered, and what a person is
  told is the reason, in words, not that "the device did not do it".
* **The fake clock had to let a task begin to wait.** A test that set a timer and moved time at
  once moved it before the keeper had looked at the clock for the first time: `FakeClock.advance`
  now lets what was just made begin first.

Nothing was measured with a model: the planner is not shown the new skills in its examples until a
graphics card is free to measure what each example moves, and the corpus gained 43 cases for the
live test to measure then.

**The first slice:** `set_mute` and `get_state`, the `data` of a result, `CoreResult.after` and the
language that a core skill is given, the skills `volume`, `volume-change`, `mute` and `volume-status`
(`factotum.actions.core.volume`, with their sentences in `core/phrases.py`), the muted state of the
speaker of the device application and of the hardware of the terminal, and the corpus and the series
of `e2e` that say the new cases to the three devices and judge them. Nothing was measured with a
model: the machine's graphics card was in use, and the planner is not shown the new skills in its
examples until it can be measured (ADR 0028 found that every example moves a case or two).
