# Writing skills

A skill teaches Factotum to do something. It is a folder with a `SKILL.md` (YAML frontmatter, then
markdown), and it is one of two kinds:

| | A script | Instructions |
| --- | --- | --- |
| What you write | a Python file, and notes | the notes, and nothing else |
| Who does the work | the script, in a container | a model writes a *plan* from the notes, the platform checks it and runs it in a container |
| Good for | anything that needs logic: parsing, loops, several services, a protocol | calling a few HTTP APIs in order, and telling a device something |
| What can go wrong | your code | the model misread the notes: it is checked, so it cannot do more than the skill was granted, but it can do less or something else within it |
| Field `entrypoint` | the script | none: that is what makes it instructions |

How the actions service reads, chooses, decides on and audits skills is in
[`actions.md`](actions.md); this page is about writing them. [`skills/`](../skills/README.md) has
eight worked examples.

## The `SKILL.md`

```markdown
---
name: movie-scene
description: Dim the lights of the living room for watching a movie.
examples:
  - set the movie scene
  - it is movie time
  - prepara la scena per il film
target: none
permissions:
  network: [homeassistant.local]
  secrets: [HA_TOKEN]
effect: reversible
---
The lights are managed by Home Assistant, at `homeassistant.local` on port 8123...
```

| Field | What it is |
| --- | --- |
| `name` | lower case letters, digits and dashes. It is what the trail calls the skill. |
| `description` | one sentence: what it does. It is embedded, and it is what the model that reads a message is shown. |
| `examples` | things a person would say, in every language you expect. They are embedded too: the more they sound like the messages, the surer the skill is found. |
| `target` | `none` (no device: a service), `system` (a Factotum device: the one that was spoken to, or the one that was named) or `external` (a lamp, a door, a TV: bound to the skill in the devices file with what is its own). |
| `parameters`, `required` | what the message may say: a name, a `type` (`string`, `integer`, `number`, `boolean`), and optionally `description`, `enum`, `minimum`, `maximum`. A required value that is not in the message is asked for; one is never invented. |
| `device_parameters` | what is fixed for an external device (its `host`, its `entity_id`): set in the devices file, never said in a message. |
| `permissions` | `network`: the hosts the skill may reach (`${device.host}` is the host of the device it is bound to; `*.example.com` is every subdomain), `secrets`: the names of the secrets it may be given. Nothing that is not declared is available, and enabling a version grants exactly what it declares. |
| `effect` | `read_only`, `reversible` or `irreversible`: how severe the worst thing it does is. It decides what a device may start (a shared speaker may not start what cannot be undone) and, for a skill in instructions mode, what a plan may do: **a plan of a `read_only` skill has `GET`s and nothing else**, whatever it says. Declare it honestly. |
| `confirm`, `approval` | when a plain yes is asked first (`never`, `ask`, `always`), and whether an out-of-band approval is needed (which is refused until one exists: ADR 0007). |
| `entrypoint` | the Python file that a script runs. Leave it out for instructions. |
| `timeout_s` | how long a run may take, 1 to 120 seconds (10). |

Then the body. For a script it is notes for people. **For instructions it is what the model follows**,
read whole every time the skill runs: at most 8000 characters.

A skill in a folder is read again with `POST /v1/skills/reload`. `POST /v1/skills` takes the text of a
`SKILL.md`, which cannot have files, so it is for instructions. Installing a version does nothing:
an administrator reads what it declares and enables it, and that is the review.

## Skills that are instructions

### How one runs

```
message ──▶ meaning search ──▶ chat.fast reads it ──▶ decision ──▶ chat.main writes a plan
                                 skill, device, values     │              │
                                                           │       the checks, in code
                                                           ▼              │
                                            yes? (a plan is kept with the question)
                                                           │
                                                           ▼
                        the runner: one container, only the network and secrets that were granted
                                     runs `executor.py` with the plan ──▶ message, commands
```

1. The message is read the way every message is (`actions.md`): which skill, which device, which
   values. That is `chat.fast`, and it never sees the notes of a skill.
2. If the decision is to go on, **`chat.main` is given the notes, the values, the *names* of the values
   of the device and of the secrets, the hosts the skill may reach and the language of the message, and
   writes a plan.** It is asked with the plan's shape as a JSON Schema, with five examples as turns of
   the conversation (one for each shape a plan comes to), and the plan starts with `reason`, because a
   small model answers better when it says why first.
3. **The checks accept the plan or refuse it.** A plan that is refused is not run: the model is told
   which step and why, in words, and asked once more (`FACTOTUM_ACTIONS_PLAN_ATTEMPTS`); if that is
   refused too, the answer is that the action failed and the trail says `no plan: <why>`.
4. If a yes is needed, **the plan is written first and kept with the question**: the person is asked
   about something that is known to be possible, and the yes is only a yes, so it is read without a
   model and the plan is checked again against what was granted a moment ago.
5. The runner starts a container, gives it `executor.py` and the plan, and the executor does what the
   plan says, step by step, and prints what a script prints: `{message, data, commands}`. The
   commands go to the device through the edge, as those of a script do.

What the model is never given: a secret (it writes `${secret.NAME}`, and the executor puts the value
in, inside the container), anything from a document or a page, and **the answer of a request**. A plan
is written once, before anything is sent; what a service answers is used by the plan and is never read
by a model. That is the one thing that keeps a page that says "ignore your instructions" from being
followed: it has nowhere to be read.

### A plan

A plan is JSON: a reason, at most eight steps, and what to say.

```json
{
  "reason": "finds the place, then reads its air quality",
  "steps": [
    {"id": "place", "kind": "http", "method": "GET",
     "url": "https://geocoding-api.open-meteo.com/v1/search?count=1&name=${value.place}",
     "headers": {}},
    {"id": "air", "kind": "http", "method": "GET",
     "url": "https://air-quality-api.open-meteo.com/v1/air-quality?current=european_aqi&latitude=${step.place.json.results.0.latitude}&longitude=${step.place.json.results.0.longitude}",
     "headers": {}}
  ],
  "say": "The air quality in ${value.place} is ${step.air.json.current.european_aqi}."
}
```

A step is one of:

* **a request**: `method` (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`), `url` (a full `http` or `https`
  address), `headers` (an object), and, except for `GET` and `DELETE`, `body`: a JSON value, which is
  sent as JSON, or a string, which is sent as text (`text/plain`), or `null`;
* **a command for a device**: `name` and `args`, one of the vocabulary of the edge (`set_volume`,
  `speak`, `play_url`, `pause`, `resume`, `stop`, `show_card`, `clear_display`, `set_led`), which
  the actions service gives to the device that was spoken to (or that the skill acts on) if it
  declared it can.

A plan is a straight line: no conditions, no loops. A request that does not give what it should (a
status of 300 or more, a connection that failed, a redirect, a value that is not there) **ends the run**
with a failure that says which step; there is no "if it failed, try the other". Say what to do in
two skills, or write a script.

### Placeholders

A plan holds no secret and none of what the person said: it holds the *place* for it, and the
executor fills it in when it runs.

| Placeholder | Is |
| --- | --- |
| `${secret.NAME}` | a secret the skill was granted, in a header, a body or an address. Never in `say` and never in a command. |
| `${device.NAME}` | a value of the device the skill is bound to (`host`, `entity_id`). It may be in the host of an address, which nothing else may. |
| `${value.NAME}` | a value the person's message gave. |
| `${step.ID.status}` | the status of an earlier request. |
| `${step.ID.text}` | the text of its answer (the first 65 536 characters). |
| `${step.ID.json.PATH}` | a field of its answer, when it is JSON: `PATH` is keys and list positions with dots between them, `results.0.latitude`. |

A placeholder can be followed by `|round`, which rounds a number (half up; a text is an error). The
model is told to use it for what it *says* (`${step.air.json.current.temperature|round}`) and never
on what the person asked for: a small model that was told only that it *can* round rounded a
temperature of 19.5 to 20 in the request it sent, so the prompt says where. The checks allow it on
any number, because a request may want one.

How a value is put in depends on where it is:

* **in an address**, it is percent-encoded, so that a value cannot end the part it is in (a place
  called `a&b` stays one value);
* **in a body that is JSON**, a string that is exactly one placeholder becomes the value with *its
  own type*: `"${value.brightness}"` is the number 40, not the text "40". A placeholder in the
  middle of a longer string is text;
* **in a header, a text body or `say`**, it is text.

A step may only refer to the answers of steps *before* it.

### What the checks decide

The model proposes and the code decides ([ADR 0014](decisions/0014-actions-the-model-proposes-the-code-decides.md),
[ADR 0016](decisions/0016-skills-that-are-instructions-a-model-writes-a-plan-the-code-decides.md)).
`factotum.actions.instructions.checks` refuses a plan that:

* reaches a host that the skill was not granted, **decided by the same function that the egress proxy
  uses** (so that "allowed here, refused there" cannot happen), or has a user name or a password
  in an address, or has anything but a value of the device in the host of it;
* has a request that is not a `GET` in a skill that is `read_only`, or a body in a `GET`;
* uses a secret that was not granted, or in a place that is not a request (`say`, a command);
* refers to a value, a value of the device or a step that does not exist, or to a step that comes later;
* sets a header that the executor sets itself (`Host`, `Content-Length`, `Transfer-Encoding`, the
  `Proxy-*` ones...), or has a control character in one;
* gives a device a command that is not in the vocabulary, with an argument that command does not
  have or a value it does not accept (checked when there is no placeholder in it);
* has no steps (a model that cannot follow the notes says so with none, and says why in `reason`).

The messages say which step and what, so that a model can act on them and a person can read them in
the trail. **They never say what a secret is**: a plan holds none.

### The trail

An action that runs a plan leaves the usual records, and `detail` says what was done, without a
value: `plan: POST homeassistant.local:8123, POST homeassistant.local:8123`. A plan that could not be
had says `no plan: the host evil.example is not one the skill may reach (it may reach: ...)`.

### Limits

| | |
| --- | --- |
| notes | 8000 characters |
| steps | 8 |
| an address | 2000 characters |
| headers | 10, each name up to 100 and each value up to 1000 characters |
| a body | 16 000 characters, before the placeholders are put in |
| what is said | 600 characters as a template, 2000 once it is worked out |
| a plan | 40 000 characters written out |
| an answer read | 1 MB; more is a failure |
| a request | 10 seconds, and all of them together within the `timeout_s` of the skill |
| what the model is given | 8000 characters of notes, five examples, and a few lines: a small model can be given all of it |

### Writing notes that a model follows

Notes are read by a model that is *good at reading and bad at guessing*. This is what was measured
(with the 8B model, [below](#what-was-measured)), and only that:

* **Write the address in full.** It is the one thing that decided the outcome. With the two addresses
  of the air quality skill written out, its plans were right 18 times in 18; with only what the two
  APIs are ("the geocoding API of Open-Meteo"), the model wrote paths that do not exist and none of
  18 worked (the checks accepted every one, because the host was the right one, and the server
  answered 404). Put what is to be filled in as words in capitals (`https://ntfy.sh/TOPIC`,
  `latitude=LAT`).
* **Say what the answer looks like, when the API is not one that the model knows.** For Open-Meteo it
  made no difference (18 in 18, with and without): the model knows what it answers. For a made-up
  API it did: the four plans written with `data.count` in the notes used it, and the four written
  without it wrote `quantity` or `results.0.quantity`.
* **A body that is text is said to be text.** "The message itself as the body (a string, not JSON)"
  was followed in 4 of 4 plans, since the schema lets a body be text (it did not before, and then no
  wording helped: see below).
* **It does not matter how the calls are laid out**, for two of them: a numbered list, a paragraph,
  and the second body said in words instead of as JSON were each right 6 times in 6. Nor does a last
  sentence that says what to say when it says only what the values already are (18 in 18 without it):
  it is for a sentence the model cannot know.
* **Name every host and put it in `permissions.network`**; the model is shown the list, and what is
  not in it is refused. `${device.host}` is for a skill that serves many devices.
* **Declare `effect` for the worst that the notes can do**, not for the usual case.
* Keep it short. Nothing in the notes is for a person to read; nothing is trusted either: they are
  the reviewed text of the skill, and they are why a skill is enabled by an administrator.

Not measured, so not advised: whether numbering helps with more than two calls, what a model does with
notes that are long (they have never been longer than a page), and any model but this one.

### Trying a skill

* **Without a runner** (`FACTOTUM_ACTIONS_RUNNER_URL` unset) a skill in instructions mode is a
  dry run, as everything is: it is found, decided on, audited, and answered with "Dry run: nothing was
  run". No model is asked for a plan.
* **With the runner and the gateway**, put the text with `POST /v1/skills`, enable it, and speak (see
  `actions.md`, *Trying it*). The trail says `plan: ...` or `no plan: ...`.
* **To see what a model writes** for your notes, and what that does against a server you control, run
  the live test (`services/actions/tests/live/test_live_instructions.py`, opt-in): it measures the
  models you name.

## What was measured

Llama 3.1 8B (q4_K_M) through Ollama, with its window at 8704 tokens and the temperature at 0 (0.3
for a second try), on an RTX 3060 Ti (8 GB) that the desktop was also using: the model is 5.9 GB of
the card and the card was at 93 to 100% while it wrote. That is the worst case that this project
measures on. `chat.main` is a role that a deployment will bind to a better model, and `chat.fast`
reads the messages.

A plan is judged by **what it does**, not by how it reads. It is run by the program that the sandbox
runs, against a web server on this machine, and what the server was asked (and with what header) and
what a device was told to say is compared with what the skill says. That is
`services/actions/tests/live/test_live_instructions.py` (opt-in). Its 22 plans are three skills that
come with the project (an announcement, a scene of two calls, a lookup of two requests over six
places) and **five that were written for the test and that nothing was tuned on**: a thermostat (a
number of degrees, one of them 19.5), a notification (a plain-text body), the state of a garage door
(a request that only reads, of a device), a coffee machine (a request, then a command) and a parcel (a
request that only reads, with a key in a header).

| | The skills of the project (12 plans) | Skills that nothing was tuned on (10 plans) |
| --- | --- | --- |
| Accepted by the checks | 12 | 10 |
| At the first try | 12 | 10 |
| **Did what the skill says** | **12** | **10** |

| One plan, from the model asked to the plan accepted | Median | 90th percentile |
| --- | --- | --- |
| The card with nothing on it but the desktop (20 plans) | 1.5 s | 2.4 s |
| The same card, a quarter of an hour later (20 plans) | 2.8 s | 4.6 s |
| Something else using the card (22 plans) | 4.8 s | 13.0 s |

Eight messages said through both models (the reading model reads the message, the writing model writes
the plan, the sandbox's program runs it): 7 were right. The eighth, *annuncia che la cena è pronta*,
was read as *Cena pronta*, and that is what the device was told to say. A message took 4.2 s (median;
the slowest 4.5 s) on the quiet card, and 8.8 s (13.5 s) a quarter of an hour later, the reading, the
plan and the run together. Both runs are of the code before the example of one request that reads
was added (below); the plans of the three skills that this test says were right 12 in 12 after it,
and the message that failed is the reading model's.

### What the measurements found

* **Accepted is not right.** The first measure said that every plan was accepted at the first try
  while the scene of two calls left out its second call, a number was written as a text ("20"), and
  `|round` was put on what the person had asked for (a temperature of 19.5 was sent as 20). None of
  those is something the checks can see, because each is a plan that a skill may make. The test
  runs each plan and looks at the server, and the prompt says where `|round` goes and the example has
  a number in it (`"transition": 2`).
* **A schema that says nothing is an object.** The plain-text body of the notification came out as
  JSON, and no wording of the notes changed it: 0 in 12 plans for each of six wordings (the notes as
  they were, "the text and nothing around it", the placeholder written out, a `Content-Type` header,
  a last sentence changed, a list), and 0 in 4 with an example of a text body, in each of three
  places in the prompt. The body was the one field whose schema was `{}` ("a value of any kind"),
  and **to the grammar that Ollama makes of a schema, `{}` is an object**: asked for the text `hello
  world` in it, the model wrote `{"hello world": ""}`, for a number `{"value": 42}`, for a list
  `{"type": "array", "value": [1, 2, 3]}`. Saying what a body may be (an object, a text, a list or
  nothing) made the notification right 4 in 4 with the notes as they were, and every one of the 40
  plans of that experiment right (in each of four ways of saying it). It was found by a skill that
  nothing was tuned on; the tests now refuse a part of the shape that has neither a `type` nor an
  `anyOf`.
* **A small model writes the shape of the examples it is shown.** With a chain of two requests as the
  only example of a request that reads, a skill that is *one* request that reads (how many jars of jam
  are left) was written with more steps than it needed (a second and a third request, called `say`,
  that repeat the first), and none of 18 plans was right, whether the answer was described, the
  last sentence said "tell the person" instead of "say", or the prompt said that `say` is never a
  step (0 in 18 each). One example of a request that reads on its own, and the same plans were right
  6 in 6, with everything else still right (46 in 46). The skill was made up to find this, so the
  parcel (which is another skill of that shape) is the one that measures whether it carries over: 2
  in 2.
* **The shape is written for a small model.** A field that may be left out is one that it leaves out
  (the address of a request); one that does not apply is one that it fills in (a body for a `GET`, a
  step inside the arguments of a command); and it calls a step `set-temp`, which is not a name (a
  name is lower case letters, digits and `_`). So a step has the fields of its kind and no others, all
  of them and in that order, and its name is a pattern that a grammar can make. With that, every one
  of the 40 plans of the experiments that wrote each of the 20 twice was accepted at the first try.
* **What the notes need** is in *Writing notes that a model follows* above, and it is not what was
  believed: the address in full, and the shape of the answer of an API that the model cannot know.
  How the calls are laid out did not matter.
* **A model does not copy what it is to say.** Of the six announcements in the corpus of messages, the
  reading model made *la cena è pronta* into *Cena pronta* and *è ora di andare* into *It's time to
  go* (translated): the plan is right and the device says something else. What a person said to be
  said aloud is worth a description that says so, and the example skill's says *as the person said
  it*, which the 8B model does not always follow.
* **A card that is in use for something else can be far slower than a shared one.** With a game on the
  same card the model wrote 1 to 3 tokens a second (60 to 70 on a card with only the desktop on it),
  and a run of a minute or two took more than ten and was stopped. The memory of the card was full
  (7.9 of 8 GB), so the weights were probably being paged; that was not looked into. The live test
  waits three minutes for each answer (`PATIENCE_S`), so that a slow card is not a wrong answer, and
  the accuracy above does not depend on it.
* **The tests that decide what a plan may do were mutated.** 103 deliberate mistakes in the code
  that says what a plan may do and what it comes to (the checks, the program that runs it, the
  matching of hosts, the contract, the service, the runner's client): the tests noticed 98, and the
  five they did not were five tests that were missing (a request that may take the whole time the run
  has instead of the rule of one request; a name that is not a host inside a domain that was
  granted; the cut of what is said of the shape of a plan and of why there was none; a script that
  would be given a plan). They are there now, and all 103 are noticed.
