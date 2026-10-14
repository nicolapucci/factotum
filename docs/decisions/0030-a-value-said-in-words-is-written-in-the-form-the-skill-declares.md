# 0030. A value that is said in words is written in the form that the skill declares

Status: accepted, 2026-10-15 (the maintainer's decision). Built: the declaration in the manifest, the
readers, the conversion and the checks, six mock skills that take their values in forms, and a
corpus for them. Not built: the skills of the platform (`timer`, `alarm`, ...) declaring their forms
(the next step), and a skill that uses the answer of another (below).

## Context

[ADR 0028](0028-the-model-alone-does-nothing-a-request-is-satisfied-by-skills-or-declined.md) made a
value one that the person said: a number has to be in the message, a text has to be made of its
words. The first measurement of the platform's skills with a model
([ADR 0029](0029-the-skills-for-what-a-device-does-are-the-interface-between-the-model-and-the-device.md))
found that a small model writes a value in the words it likes (*sette e mezza* as `7:30` and as
`seven thirty`, *dieci minuti* as `ten minutes`, *domani* as `tomorrow`), that the check left them out,
and that what it cost was silent when the value was optional (*cancel the five minute timer* cancelled
the next timer). The skills of the platform got round it by taking *the words* and reading them
inside the skill (`core/when.py`); a skill of a person has no such way out, and a manifest could not
say what it wanted.

The maintainer's decision, 2026-10-15: *the values are converted into the format that the skill
requires; the skill declares it in its manifest (a specific format of a time, say) and the system
converts what is said by voice into it.* And: *a skill can offer more than one way to indicate a date
(`16/10/26`, or in one day).* And for later: *the model will be able to create a temporary skill that
calls the other skills and uses their outputs*, so that the date comes from a method of the clock and
is handed to the method that sets the alarm.

## Decision

- **The manifest declares the forms.** A parameter may have `formats`: one or more of `time` (a time
  of day: `HH:mm`, `h:mm a`), `duration` (a length of time: `seconds`, `minutes`, `hours`, `iso`),
  `date` (`dd/MM/yy`, `yyyy-MM-dd`), `in_days` (the number of days from today: `{n}`, `+{n}d`) and
  `weekday` (`name`, `iso`). The vocabulary of patterns is closed and small
  (`factotum.contracts.formats`): a skill cannot bring a reader or a writer of its own, so what it
  declares is data, checked when the manifest is read. A pattern that means nothing, does not fit
  the type of the parameter, is mixed with a form of another group or sits beside a list of
  choices stops the manifest and says why.
- **The model points, and code reads and writes.** The model is shown the value as *a time of day,
  as the person said it* and writes the words (or a form of them: `7:30` is as good as *sette e
  mezza*, and so is `tomorrow` for *domani*). Code reads what it wrote, looks for the same thing in
  what was said, and writes it in the form the skill declared (`factotum.actions.formats`).
- **A value is said when its reading is one of the message's.** The message is searched in windows
  of its words (`core.when` reads a time, a length and a day in Italian and in English, and a date
  in digits), and only the windows that are not inside a longer one count: *sette* is not said in
  *sette e mezza*, and neither is 7:00; the *dieci* of *tra dieci minuti* is not an hour. A model's
  value that no window of the message reads as is not a value: a made-up `600` for *cinque minuti* is
  the case ADR 0028 was written for, and it stays refused. A model that copied only part of what was
  said (*alle nove* for *stasera alle nove*, *sette* for *sette e mezza*) pointed at it, and the
  person said the whole: the one thing that is said where it pointed is the value, unless it is said
  in two places that say different things.
- **What cannot be written is not a value.** The platform has no clock (ADR 0029), so *domani* is
  `+1d` for a skill that counts days and nothing for one that wants a calendar date; a date with
  no year is not `dd/MM/yy`; 90 seconds are not a whole number of minutes. Not said, not
  understood and not writable are all *unsaid*: the skill is called without the value if it is
  optional and the person is asked for it if it is required. A written value outside the bounds of
  its parameter is invalid, as any value is.
- **A day is one thing in three forms.** A day that is said is *in some days from today*, *a day of
  the week* or *a date*, which is how the alarms of ADR 0029 already read it (`AlarmDay`), and each of
  the forms `in_days`, `weekday` and `date` writes one of them. A skill that declares several takes
  the day in whichever way the person said it. It is the skill that says which ways it takes, and the
  system converts into one of them.

## What was considered

- **The model writes the form.** Rejected: that is the model doing sums (a time, a number of seconds,
  the number of days to a weekday), which is what ADR 0028 refuses; and it gets them wrong.
- **The platform turns *tomorrow* into a date.** It has no clock and no time zone; the device has
  both, and asking it for the date to write a request would make every request that says a day wait
  for a device. A skill that counts days is given the days; one that wants a calendar date has its
  person asked for the day, until a skill that has the clock can be asked first (below).
- **Code reads the message by itself and the model is not asked.** A message can hold two times (*dalle
  sette alle otto*), and the model says which one is for which parameter; code only checks and
  writes. The model proposes, the code decides (ADR 0014).
- **Any window of the message counts.** *Sette* would be said in *sette e mezza*, and a model that
  forgot the half would be believed.

## Consequences

- The sentence of ADR 0028 that a text is made of the words of the message is true of every text that
  has no format. For a value with formats the rule is that the person said it, in whatever form the
  words, the digits or the model's own reading take.
- The platform's own skills can stop reading words inside the skill and declare what they take (the
  next step), and the sentence of the prompt that asks the model to copy the words and never to
  write `7:30` can go: both are accepted.
- **Later, a skill that uses the answer of another.** The model creating a temporary skill that asks
  the clock for the date and hands it to the alarm is the other half of this: a skill that wants a
  calendar date is given one by a skill that has the clock. It is a decision of its own (skills calling
  skills, ADR 0026), and one that has to square with ADR 0016, by which a model never reads what a
  service answers: the natural shape is a plan with a placeholder that code fills in, and the value
  that fills it goes through the same writing as a value that was said. Nothing here closes it.
- A year said in words (*duemilaventisei*), a date whose month is a number and whose day is a word,
  and a time of day with a period in the other language than the rest are not read; they are asked for.

## What was built, and what it found

The declaration (`factotum.contracts.formats`, `ParameterSpec.formats`), the readers over split words
(`core/when.py`: `read_time`, `read_day` with the year of a date, `read_duration`), the conversion and
its windows (`factotum.actions.formats`), the check that uses it (`values.check_specs`), what the
model is shown (`planner._value`), six mock skills that take their values in forms and 62 messages for
them. Measured with the 8B model: all 38 messages that were written first are right (32 of 38 before
what the first run found), and 24 written afterwards and said once were all right the first time.
What the first run found was a model that copies only part of what was said and one that leaves out
the word that begins a day; both are handled, and a judge that was strict about an article was
corrected. The numbers and the list are in
[`actions.md`](../actions.md#values-in-the-form-a-skill-declares-adr-0030-six-mock-skills-62-messages).
