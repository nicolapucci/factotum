# Mock skills

Skills that do nothing and answer as they would, to see how the assistant **chooses** a skill and
**calls** it (the skill, the values, what it asks when one is missing, what it leaves alone when
nobody said it, what it refuses and what it asks a yes for), without anything having to work. They are
written plainly, as the author of a skill would write them, and nothing the planner is shown is about
them: this is how it does with a skill it has never seen.

Each is a folder with a `SKILL.md` (the manifest the system expects, with no script: there is nothing
to run) and a `mock.yaml` that says what it answers, in Italian and in English:

```yaml
replies:
  - when: {room: "*"}               # "*" is any value that was given; anything else is equal to it
    it: "In {room} ci sono 21 gradi."
    en: "It is 21 degrees in the {room}."
  - it: "In casa ci sono 21 gradi."  # the last one is for whatever is given
    en: "It is 21 degrees at home."
```

The first reply whose `when` fits is the answer, in the language of the person; the blanks are the
values the skill was called with. A read answers with something plausible (a weather, a score, a
temperature), an action says it is done and what it was done with.

Some of them take a time, a length of time or a day in a form that their manifest declares
(`formats`: `HH:mm`, minutes, `+1d`, ADR 0030). The person says *sette e mezza* and the reply shows the
form that the skill was given (*alle 07:30*), which is how to see what the system made of what was said.

## Trying them

Where no runner is set, `FACTOTUM_ACTIONS_MOCK_RUNNER=true` makes the actions service answer a skill
from its `mock.yaml` (development only; a skill with none is answered as the dry run does). With
`FACTOTUM_ACTIONS_SKILLS_DIR=skills`, `POST /v1/skills/reload` installs these along with the
examples, and then *imposta il termostato della cucina a venti gradi* comes back as *Ho impostato il
termostato di cucina a 20 gradi.*, and the audit trail says which skill ran with which values.

The messages that these skills are measured with are in `factotum.actions.mock_cases`; the live test
that says them to a real model is `services/actions/tests/live/test_live_actions.py`.

## What is here

| | |
| --- | --- |
| `thermostat-set`, `thermostat-status` | a number in a range with a room if one is said, and the read of it |
| `fan-speed` | a choice from a list |
| `blinds-set`, `blinds-move` | a percentage and an open or a close: two skills, as the volume is |
| `shopping-list-add`, `reminder-add` | words that were said, and a time if one is said |
| `tv-channel` | an integer, said in digits or in words |
| `music-play` | acts on the device spoken to; what to play only if said |
| `door-lock`, `send-message` | cannot be undone: a shared speaker may not, the others ask for a yes |
| `coffee-brew` | a choice and a count that may be left out |
| `vacuum-clean` | everything optional, a room from a list in the other language |
| `night-mode` | a yes or a no |
| `fridge-temperature` | a number with a decimal |
| `sports-score` | a read of a team |
| `oven-preheat` | a number in a range and a mode, near the thermostat |
| `meeting-add` | a day in three forms (`16/10/26`, `+1d`, `monday`) and a time (`HH:mm`) |
| `tv-record` | a time (`HH:mm`) and a length in minutes, beside a channel |
| `tv-sleep` | a length as an ISO 8601 duration (`PT1H30M`) |
| `train-ticket` | a day only as a calendar date (`2026-10-16`): *tomorrow* is asked for |
| `heating-schedule` | two times in one message (*dalle sei alle otto*) |
| `sprinkler-run` | a length in seconds, in a range |
