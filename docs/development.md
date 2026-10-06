# Development

## Setup

You need Git, Docker, and [uv](https://docs.astral.sh/uv/) (on Windows: `winget install
astral-sh.uv`). uv finds a Python 3.13 or downloads one.

```bash
uv sync --all-packages      # creates .venv from uv.lock
cp .env.example .env        # then set FACTOTUM_POSTGRES_PASSWORD
```

A `.env` in the repository is read by the services and not by the tests: every test runs from an
empty directory, so what it finds does not depend on the passwords, keys and environment of the stack
you have up.

## The checks

CI runs exactly these, so run them before committing:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest -m "not integration and not live" --cov
```

Optionally install the git hooks: `uv run pre-commit install --hook-type pre-commit --hook-type
pre-push` (ruff and mypy on commit, the unit tests on push).

## Layout

```
libs/contracts/   pydantic contracts shared by every service: the single source of truth
libs/common/      authorization, redaction and logging, settings, the SSRF guard, development auth,
                  and the hub's tokens and passwords, used by every service that checks one
libs/devices/     the SystemDevice interface, a fake for tests, and the file of devices of a home (development)
skills/           example skills (folders with a SKILL.md and their scripts)
libs/web/         what the HTTP interfaces share: errors, authentication, body limits
libs/db/          the Postgres pool, migrations and the harness for tests against a real database
libs/model_client/ how a service reaches the model gateway: chat, streamed chat, embeddings, profiles
libs/client_core/ what every client of the edge shares: the protocol, the sound of an answer, the
                  conversation by voice, staying connected, and the ports that hardware plugs into
libs/client_audio/ the sound hardware that clients share: a microphone, a speaker with a volume,
                  where speech ends, a key and a wake word as the ports of a client, and a sound
                  card that a test plays
services/         one folder per service (so far: model_gateway, knowledge, conversation, edge, actions, runner, egress, hub)
clients/          the devices that talk to the edge, made of the client core (so far: terminal, a
                  device in a terminal, and device, the program of a speaker with a microphone for a
                  PC and a Raspberry Pi)
e2e/              the end-to-end tests, and the rig they share: every service in one process,
                  from a device to the models
config/           example configuration, such as the model bindings
docs/             design.md, the decision records, one document per service
infra/            files mounted into containers
```

Every member of the workspace lives in the PEP 420 namespace `factotum`, so `libs/common` provides
`factotum.common`. Services join under `services/` and clients under `clients/`.

To add a member, create its folder with a `pyproject.toml` (copy one from `libs/`), add it to
`[tool.uv.workspace]` if it is outside `libs/*`, `services/*` and `clients/*`, add its `src` to
`mypy_path`, `ruff`'s `src` and the coverage `source` in the root `pyproject.toml`, and run
`uv sync --all-packages`.

## Conventions

- Every change ships with unit tests, written together with the code. Unit tests never use the
  network; use fakes and `httpx.MockTransport` or `respx`.
- Shared shapes are `Contract` subclasses in `factotum.contracts`: immutable, and they reject unknown
  fields. Use the typed ids (`TenantId`, `DeviceId`...) rather than bare UUIDs.
- Builders for tests live next to the contracts (`factotum.contracts.testing`).
- Errors derive from `FactotumError`, end in `Error` and carry a stable `code`.
- Commits are small and conventional; the body says why.

## End-to-end tests

`e2e` holds the tests that need every service at once, and the rig they share (`factotum.e2e`, a
member of the workspace like the others, since one test module cannot import another). They need no
model and nothing beyond the loopback (and no Docker, in memory), and take a second or two each:
more than a unit test, and far less than the stack in containers. They cover what a unit test does
not: a client of the client core (with the hardware of the terminal) talks over a real WebSocket
to the edge, and behind the edge
the conversation, knowledge, actions and model-gateway applications are the real ones, each reached
through its own HTTP interface. Nothing is replaced above the stores, which are in memory or, as in
production, the Postgres of `docker compose up` (a database of its own for the test, prepared by the
migrations the services run): every test runs on both, and the second is an integration test. The
home is the example one, `config/devices.example.yaml`, with the skills of `skills/examples`.

What is made up is what a unit test cannot have:

- **The hardware of the device.** A recording is a WAV file that carries the words that were said in
  it, the speaker remembers what it was asked to play (a clip, or the sound as it was given it:
  `FakeVoiceOutput`, `FakeSpeaker`), and the wake word is a `FakeVoiceInput` that hands the client one
  recording (or one stream) after another. Sound gets to the edge whole and comes back as a clip in
  each sentence, or goes in as the chunks a microphone sends as a person talks (ADR 0022) and comes
  back as a stream that the speaker is given as it arrives (ADR 0023): see below.
- **The models.** Whisper and Piper are the real adapters of the gateway on top of stand-in engines
  (the first reads the words out of a recording, the second writes a clip that carries the words it
  was asked to say, so a test reads what a speaker was made to say). The chat model answers from the
  sources it was shown, the embedding model gives words a direction, and the small model that plans
  reads a message as a question or as a command. What it proposes is a proposal: it names a skill or
  a device whether or not they were offered, and the tests show that the code decides.
- **What a skill reaches, and the container it runs in.** A script is the real one, run in a process
  of its own with what it was granted (`LocalRunner`); the Home Assistant it calls is a web server on
  this machine that every host resolves to, which keeps what it was asked (`FakeWeb`).

```bash
uv run pytest e2e                         # in memory, and in Postgres where there is one
uv run pytest e2e -m "not integration"    # in memory only
```

Every test runs three ways: with the services keeping what they keep in memory and the device
carrying sound as clips (a recording sent to the edge whole, the voice a clip in each sentence:
`memory-clip`), the same with it carried as streams (the recording as a microphone sends it, the voice
as a stream that the speaker is given as it arrives: `memory-stream`), and in the Postgres of
`docker compose up` with streams (`postgres-stream`, an integration test). What a store does and how
sound gets to the edge and back have nothing to do with each other, so they are not run in every
combination (the fixture is in `e2e/tests/conftest.py`, and
a module that is about one way overrides it). A device says what the edge understood of what it was
told (`Device.understood()`: the `heard` frames it was sent), so a test can look at what the
assistant made of a recording and not only at what it did.

They cover a question answered aloud from the documents (asked by voice, or typed, on a speaker and
on a screen with a speaker), what was heard being what the conversation keeps, the wake-word loop of
the terminal, who may hear which document read out (a person's own phone against the shared speaker),
the longest recording a microphone makes, Italian with its accents, and each thing that may be
missing (no speech-to-text model, no text-to-speech model, a silence, a model that falls over).

A person speaking over the answer is in `test_e2e_speaking_over.py`: the stand-in model goes quiet
in the middle of an answer, a stream begins, and the answer is stopped at once, all the way back to
the model that was writing it, before the person has finished; what they say is heard and answered;
and one who begins and goes quiet is told (`stream_stalled`) and may begin again. What the rig shows
of an answer that is held is nothing, whatever was written: the services are put together with
in-process transports, which hand a response over when it is whole (the sentence by sentence arrival
of an answer is shown on the real sockets of the edge's own tests).

The voice of an answer that is streamed out (ADR 0023) is in `test_e2e_voice_out.py`, which reads
what the edge sent through a **probe** (`factotum.e2e.probe`): a device seen from the wire, with no
client library between the test and the edge, that says hello with `voice_stream` (or without), says
a question the way the carrier of the test does and keeps every frame it is sent. It shows the words
and their sound in the order the protocol promises (a track of chunks of a tenth of a second, whole
samples, each labelled with its part, ended before the turn), a device that did not ask for a voice
being sent clips, a house with no voice bound, a voice that is made slowly (the stand-in of Piper can
be held: the words do not wait for it) and a person who speaks over it (the sound stops where it
is). `test_e2e_voice_wire.py` serves the gateway on a real socket, which the services of the house
are not, and shows what they cannot: a sentence arrives before the next is made, and a listener that
goes away leaves the voice where it was.

They cover what is asked to be done too, in the example home: a lamp switched through Home Assistant
with the token of the home and a script that was given only what it declared, the volume of the
device that was spoken to, a garage door that the kitchen speaker is refused (in English and in
Italian) and the living room display asks about, a yes that opens it, a no, a yes that adds
something, a yes that is taken once, a yes said to another device, a skill that was switched off
meanwhile; a lamp that is one person's own and that the shared speaker is never told of; a question
that names something to do, which is answered and runs nothing and whose planner never reads a
document; and the audit trail of it all, with the device each step came from.

**A series of commands** (`test_e2e_series.py`, `factotum.e2e.series`) says the corpus that a model
that plans is measured against (`factotum.actions.plan_cases`: 82 messages in English and in
Italian, about the example skills, said to the kitchen speaker, the living room display and a
phone) one after another, to the three devices at once, each on the one connection it holds for the
whole series, and four more where the corpus is silent (a phone that has two lamps to choose from, a
speaker that is told of a lamp that is not its own). Each message comes with what the code must make
of it: done, asked about first, refused, asked for what is missing, or left as a question. What is
looked at, for every one, is what the whole assistant did: what the actions service made of it (it
is recorded as it is said: `House.acts`), the skill that ran and what it was given (the values, the
hosts it may reach, the secrets it was granted and no others), what Home Assistant was asked, what
each device was told to do and has done (a volume, an announcement on the device that was named and
no other), and, after all of it, the audit trail, which must say exactly what was done, refused and
asked about and nothing else, and every client, which must have seen no rule of the protocol broken
and every voice that was streamed to its end. A question that asks first is answered with a yes at
the display, which may start what cannot be undone, and with a no at the phone, which leaves the
door shut. The planner is a stand-in that proposes what a competent one would for the message that
was *meant* (`HouseholdModel.proposal`, the proposal for *now*, never a queue: a message that does not
reach the planner leaves nothing for the next), and a model that writes the plan of a skill that is
only instructions does the same (`HouseholdModel.writing`, as strictly as the gateway checks it); the
planner is shown every skill (`running(offer_every_skill=True)`), since the stand-in that gives words
a direction cannot tell that "bump it up to eighty" is near the volume. What is real is everything
after it, and that is what breaking the code on purpose shows: ten ways (a yes that is not asked for,
a device that may start what it may not, a command given to the device that was spoken to and not
the one that was named, commands that are not given, the first device taken for the one named, the
values that a skill does not require dropped, a trail that forgets what was answered, a hearing that
is told something else, a hardware that does not take its volume, a message that lacks a value run
all the same) were each noticed, one of them only once the series had a phone with two lamps. The
skill that reaches the weather does it over https, which the stand-in of the web does not speak, so
the runner of the house prints what it would have (`HouseRunner.canned`) and keeps the job it was
given.

**The device application** is run against the same house in `test_e2e_device_app.py`
(`factotum-device` as it will be on a PC or a Raspberry Pi, with a sound card and a key that a test
plays and the edge on a real socket): a person presses the key and says a question (the blocks the
microphone hands over carry the words of the stand-in speech), the answer comes back as the voice of
the assistant and is given to the speaker as it arrives, a command that a skill answers with is run
by the hands of the device (a volume that is turned down), a text to say is what the device cannot do
and the skill is told so, a credential that the edge does not know is refused and not asked for
again, and the doctor says which setting to look at. It runs in memory and in Postgres. What a sound
card does is the audio library's to test, and what a person hears is for a person who runs
`factotum-device doctor --play`.

A document can be a file: a PDF is uploaded as a person uploads it, read by the knowledge service in
a process of its own, and answered from with the page it is on; a Word file with its heading; an
Italian one with its accents from the page to the speaker; a PDF that is broken fails where it was
read and the service goes on; and a file is read for what its bytes are, not for what it is said to
be.

Run the way production runs, with the hub (`running(auth=Auth.HUB)`), nothing reads a header that
says whom a call is for: the services share a secret, check the signed token they are shown and
mint a short one of their own to call the next, and the devices, the skills bound to them, the
models and the secrets of the home are the hub's, paired in it by its administrator the way
[`hub.md`](hub.md#trying-it) says. The journeys above are run against it, and so is what only the
hub has to say: a device with the placeholder credential of the example file, or with another
device's, is refused, and every service refuses a call with no token, with one that another secret
signed and with the headers of development.

They do not cover yet: the containers (the sandbox of the runner has its own tests that need an
engine), an approval (there is nobody to ask yet), and two tenants (every service is given one
tenant, so what they show is the owners of a tenant: a person, and the tenant itself for a shared
device).

### With real speech

`e2e/tests/test_e2e_speech.py` is the same house with the real Whisper and the real Piper where the
stand-ins are, for what only a real model can show: what is heard of a voice and in which language,
what is understood of it and attempted because of it (a lamp, a number, a door that asks first and
opens at a spoken yes, a door that a speaker is refused, a silence), and how what the speaker is
made to say sounds to somebody who listens, which is a Whisper again. The person who speaks has a
voice of Piper's of their own, which says the same words the same way each time and reaches the
device the way a microphone hands it over (16 kHz). What writes the answers and plans the actions is
still made up, and so is what the skills reach: the right thing having been attempted is what is
tried, not that it could be done. The tests about the garage door give the stand-in of the planner
what a competent one would propose (`HouseholdModel.proposal`) instead of leaving it to read what
Whisper heard, because what they are about is what is done with a door (it asks, a yes opens it, a
no and a "maybe" do not, a speaker is refused), and Whisper hears "garage" said the Italian way as
something else (below): they used to pass whatever it heard, since a door that nobody understood
was a door that stayed shut.

They are `live` tests, run by a person and never in CI, and nothing is ever downloaded. They run
with the recording streamed, the way that will be the real one: a real model hears the same samples
whichever way they came, since they are put together before it listens.

```bash
FACTOTUM_LIVE_TESTS=1 uv run pytest e2e/tests/test_e2e_speech.py -m live -s
FACTOTUM_LIVE_TESTS=1 FACTOTUM_LIVE_SPEECH_DEVICE=cuda uv run pytest e2e/tests/test_e2e_speech.py -m live
```

The models are looked for where they already are, and the tests skip where they are not:
`FACTOTUM_LIVE_WHISPER_MODEL` names the folder of a faster-whisper model (without it, the biggest of
the snapshots of `Systran/faster-whisper-large-v3`, `-medium` and `-small` in the Hugging Face cache)
and `FACTOTUM_LIVE_PIPER_VOICE` the `.onnx` file of a voice, with its `.onnx.json` beside it (without
it, `it_IT-paola-medium` in `~/.cache/factotum/piper`). A name is not a folder: it would be fetched,
so it is not found. `FACTOTUM_LIVE_VOCABULARY` lists words, separated by commas, that the binding of
speech-to-text gives Whisper (none unless it does: [below](#with-a-vocabulary)). A processor takes seconds for each thing heard: the ten tests above take a
minute and a half with `small`, two and a half with `medium` and four with `large-v3`; on the card,
ctranslate2 needs cuBLAS 12 on the `PATH` (`FACTOTUM_CUDA_DLL_DIR`, or the folder Ollama keeps its
own in, which the rig finds), and a card that something else is using is slower than the
processor. A real model hears a voice a little differently on each device, so what is looked for in
what it heard is the stem of a word and how much alike two sentences are, never a sentence.

The first runs found what the stand-ins had hidden: the PyAV that was locked could not decode a
single recording (`av` is held below 19), Whisper left to guess the language took a short command
for Arabic or Polish half of the time (`options.languages`), and told to hear Italian it wrote the
credits of a subtitle for a second of silence (what is not speech is now filtered out before it
listens). See [`model-gateway.md`](model-gateway.md#speech).

#### A series of commands, said in Italian

`e2e/tests/test_e2e_speech_series.py` says the Italian part of the series above (26 commands: lamps,
the volume, the garage door, the weather, the air, an announcement, a scene for a movie, the router,
and questions that look like them) to the three devices with that voice, through the real Whisper,
the way a microphone hands it over, and judges everything that the series with stand-in speech
judges, for the message that was *meant*. What it adds is what was *heard*: each command must be
nearer to what was said than to any other of the series (a reader who knows the commands takes each
for the one that was said), a yes and a no are heard as what they are, and what the planner was
shown to read is what the device was told it was taken for. `-s` prints a line for each command,
with how alike what was heard is to what was said.

```bash
FACTOTUM_LIVE_TESTS=1 uv run pytest e2e/tests/test_e2e_speech_series.py -s
FACTOTUM_LIVE_TESTS=1 FACTOTUM_LIVE_WHISPER_MODEL=/path/to/a/model uv run pytest e2e/tests/test_e2e_speech_series.py -s
```

Measured on the processor of this machine (2026-10-05), with the 25 commands of the series that were
there when the three were compared (the same Piper voice for all):

| Whisper | On disk | Time | Heard as said (alike ≥ 0.95) | Mean | Heard worst |
| --- | --- | --- | --- | --- | --- |
| `small` | 0.46 GB | 1 min 40 | 15 of 25 | 0.91 | "apri il garage" as "Appigara!", "alza il volume a 70" as "Adds al volume 60", "riavvia il router" as "e avvia l'ute." |
| `medium` | 1.5 GB | 3 min 28 | 16 of 25 | 0.92 | "riavvia il router" as "Ja, via Lutte!", "apri il garage" as "A pigara!" |
| `large-v3` | 2.9 GB | 6 min 14 | 20 of 25 | 0.96 | "riavvia il router" as "e a via lute", "apri il garage" as "A Pilgara" |

What it shows. The bigger the model, the more commands are heard as they were said, and a number,
which is what a command like the volume is about, was wrong only with `small` ("a 70" as "a 60"). A
command that Whisper writes another way is not a command heard wrong ("metti la luce al 50 per
cento" came back as "al 50%" from all three). And two commands are heard badly by every size: "apri
il garage" and "riavvia il router", the two with a word of English that the voice says the Italian
way. A person may say them in a way that Whisper knows better than this voice does, and nothing
here shows whether they do; what is certain is that a command is not understood because it was
heard well once. The words of the home can be given to the model (below), which is what to try if a
real microphone does the same. It is also why the rig takes the biggest model it finds.

##### With a vocabulary

`FACTOTUM_LIVE_VOCABULARY` (words, separated by commas) has the speech-to-text binding of the house
list them for Whisper to write as they are (`options.vocabulary`,
[`model-gateway.md`](model-gateway.md#speech)), so the same series says whether that helps with the
models and the voice of a machine. Measured the same way (2026-10-06, the processor of this machine,
the 26 commands that the series has now, one more than the table above had), with ten words of the
example home (`garage, router, wifi, lampada, salotto, camera da letto, cucina, volume, meteo, Home
Assistant`):

```bash
FACTOTUM_LIVE_TESTS=1 FACTOTUM_LIVE_WHISPER_MODEL=/path/to/a/model \
    FACTOTUM_LIVE_VOCABULARY="garage,router,wifi,lampada,salotto,camera da letto,cucina,volume,meteo,Home Assistant" \
    uv run pytest e2e/tests/test_e2e_speech_series.py -s
```

| Whisper | Vocabulary | Time | Heard as said (alike ≥ 0.95) | Mean | Heard worst |
| --- | --- | --- | --- | --- | --- |
| `medium` | none | 3 min 58 | 17 of 26 | 0.927 | "riavvia il router" as "Ja, via Lutte!", "apri il garage" as "A pigara!" |
| `medium` | ten words | 4 min 00 | 21 of 26 | 0.951 | "riavvia il router" as "Vi avvi a l'ulte.", "apri il garage" as "Apri gara." |
| `large-v3` | none | 6 min 20 | 21 of 26 | 0.958 | "riavvia il router" as "e a via lute", "apri il garage" as "A Pilgara" |
| `large-v3` | ten words | 6 min 26 | 23 of 26 | 0.961 | "riavvia il router" as "E a via lute!", "apri il garage" as "Appilgara" |

What it shows. Words that Whisper did not know how to write are written: "router" in a question was
"looter" and "Luther" and is "router" with both sizes, and the loanword commands are nearer to what
was said with `medium` (0.64 to 0.78 and 0.62 to 0.73) and are not right. With `large-v3`, which
already wrote "router" and "garage" when it heard them, there is less to gain (two commands more) and
no loanword command is better. The cost is not in the series: it compares the first word of a yes and
a no, and a lone "sì" that `large-v3` heard back as "Sì, va bene." with the list of words, and as a
sentence made of the words of the home when the prompt was a sentence, was found with the engine
itself in a throwaway script (`medium` heard "Sì." both times), which is why nothing is listed unless
a binding says so.

## Trying the model gateway

See [`model-gateway.md`](model-gateway.md#running-it). To run its tests against a real Ollama (opt-in,
never in CI):

```bash
ollama serve
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/model_gateway/tests/live -k ollama
```

The tests for the hosted providers run with your own keys in the environment (`-k hosted`).

To measure the conversation service with real models (a small model reading whether messages refer
to the past, and a whole conversation over real documents and a real database), opt-in as well;
[`conversation.md`](conversation.md#trying-it) says which models it uses and how to change them:

```bash
FACTOTUM_POSTGRES_PASSWORD=dev FACTOTUM_LIVE_TESTS=1 \
    uv run pytest -m live services/conversation/tests/live -s
```

To measure the actions service with real models (a small model telling commands from questions in
English and Italian, how a yes or a no is read, and how near commands are to their skills by
meaning), opt-in as well; [`actions.md`](actions.md#tests) says how to change the models:

```bash
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/actions/tests/live -s
```

Services run in containers with `docker compose --profile services up -d --build --wait`; a plain
`docker compose up` starts only the database.

## Trying the edge

The edge is where devices connect, and `factotum-terminal` is a device in a terminal. Both are
described in [`edge.md`](edge.md#trying-it); with the whole stack running (the edge reads its devices
from `config/devices.dev.yaml`, which git ignores):

```bash
cp config/devices.example.yaml config/devices.dev.yaml
FACTOTUM_POSTGRES_PASSWORD=dev FACTOTUM_ENVIRONMENT=development \
    docker compose --profile services up -d --build --wait
uv run factotum-terminal --profile smart-display      # or speaker, screen, player, phone
```

With no hardware and no key, a recording in a file can be said to it and what the assistant says
kept (the client that tests the whole stack; see [`edge.md`](edge.md#the-terminal-client)):

```bash
uv run factotum-terminal --profile speaker --wav-in question.wav --wav-out answer.wav
```

The tests of the terminal run against a real edge served by uvicorn on a loopback port, so they need
no Docker.

The program of a real device, with a push to talk (Enter) and no key for a wake word, is described in
[`device.md`](device.md#trying-it): `uv run factotum-device init`, `doctor`, then run it.

## Trying the actions

The actions service reads the same `config/devices.dev.yaml` as the edge (the devices, and which
skill drives which lamp or door) and `config/secrets.dev.yaml` (the secrets skills are given), and
`skills/examples` are the skills to try. In development the folder of skills can be enabled as it is
read; [`actions.md`](actions.md#trying-it) has the commands. What runs a script is the runner, in a
container of its own per run, so **the engine must be running** and the stack needs a key for the
egress proxy (`FACTOTUM_EGRESS_KEY` in `.env`, 32 characters or more:
`python -c "import secrets; print(secrets.token_urlsafe(48))"`); [`runner.md`](runner.md) says how
it works.

```bash
cp config/devices.example.yaml config/devices.dev.yaml
cp config/secrets.example.yaml config/secrets.dev.yaml     # put real values in it; git ignores it
FACTOTUM_EGRESS_KEY=... FACTOTUM_ACTIONS_ENABLE_SKILLS_DIR=true FACTOTUM_POSTGRES_PASSWORD=dev \
    FACTOTUM_ENVIRONMENT=development docker compose --profile services up -d --build --wait
curl -s -X POST http://127.0.0.1:8085/v1/skills/reload      # installs and enables the examples
uv run factotum-terminal --profile smart-display            # say: turn on the lamp
```

The tests of the runner that need a real engine (they run scripts in containers and look at them
from the inside) skip where there is no socket, which is Windows; they run in a Linux container that
is given one, and in CI: [`runner.md`](runner.md#tests) has the command. So do the ones that run the
program that runs the plans of skills that are only instructions
(`services/actions/tests/test_actions_executor_sandbox.py`); how to write such a skill, and what a
small model makes of it, is in [`skills.md`](skills.md), and the live test that measures it is
`FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/actions/tests/live -s -k live_instructions`.

## Trying the hub

The hub needs a token secret of its own in every environment, including development: without one it
does not start. [`hub.md`](hub.md#trying-it) has the commands (logging in as the administrator that
is made at the first start, changing its password, adding a user).

```bash
FACTOTUM_HUB_TOKEN_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))") \
FACTOTUM_POSTGRES_PASSWORD=dev FACTOTUM_ENVIRONMENT=development \
    docker compose --profile services up -d --build hub --wait
```

Giving the same secret to the other services (above) lets a token from here authenticate a call to
any of them, in place of the development headers.

## Database

```bash
FACTOTUM_POSTGRES_PASSWORD=dev docker compose up -d --wait
docker compose exec postgres psql -U factotum -d factotum
docker compose down -v      # also removes the data volume
```

The database listens on `127.0.0.1:${FACTOTUM_POSTGRES_PORT:-5432}` only. The pgvector extension is
created the first time the database starts (`infra/postgres/initdb/`).
