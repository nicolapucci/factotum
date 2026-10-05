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
services/         one folder per service (so far: model_gateway, knowledge, conversation, edge, actions, runner, egress, hub)
clients/          the devices that talk to the edge (so far: terminal, a device in a terminal)
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
not: the terminal client talks over a real WebSocket to the edge, and behind the edge
the conversation, knowledge, actions and model-gateway applications are the real ones, each reached
through its own HTTP interface. Nothing is replaced above the stores, which are in memory or, as in
production, the Postgres of `docker compose up` (a database of its own for the test, prepared by the
migrations the services run): every test runs on both, and the second is an integration test. The
home is the example one, `config/devices.example.yaml`, with the skills of `skills/examples`.

What is made up is what a unit test cannot have:

- **The hardware of the device.** A recording is a WAV file that carries the words that were said in
  it, the speaker remembers what it was asked to play, and the wake word is a `FakeVoiceInput` that
  hands the terminal one recording after another. That is the mock of the client: a client that
  streams audio and can be interrupted (ADR 0021) will need a stand-in of its own.
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

They cover a question answered aloud from the documents (asked by voice, or typed, on a speaker and
on a screen with a speaker), what was heard being what the conversation keeps, the wake-word loop of
the terminal, who may hear which document read out (a person's own phone against the shared speaker),
the longest recording a microphone makes, Italian with its accents, and each thing that may be
missing (no speech-to-text model, no text-to-speech model, a silence, a model that falls over).

They cover what is asked to be done too, in the example home: a lamp switched through Home Assistant
with the token of the home and a script that was given only what it declared, the volume of the
device that was spoken to, a garage door that the kitchen speaker is refused (in English and in
Italian) and the living room display asks about, a yes that opens it, a no, a yes that adds
something, a yes that is taken once, a yes said to another device, a skill that was switched off
meanwhile; a lamp that is one person's own and that the shared speaker is never told of; a question
that names something to do, which is answered and runs nothing and whose planner never reads a
document; and the audit trail of it all, with the device each step came from.

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
tried, not that it could be done.

They are `live` tests, run by a person and never in CI, and nothing is ever downloaded:

```bash
FACTOTUM_LIVE_TESTS=1 uv run pytest e2e/tests/test_e2e_speech.py -m live -s
FACTOTUM_LIVE_TESTS=1 FACTOTUM_LIVE_SPEECH_DEVICE=cuda uv run pytest e2e/tests/test_e2e_speech.py -m live
```

The models are looked for where they already are, and the tests skip where they are not:
`FACTOTUM_LIVE_WHISPER_MODEL` names the folder of a faster-whisper model (without it, the snapshot of
`Systran/faster-whisper-small` in the Hugging Face cache) and `FACTOTUM_LIVE_PIPER_VOICE` the `.onnx`
file of a voice, with its `.onnx.json` beside it (without it, `it_IT-paola-medium` in
`~/.cache/factotum/piper`). A name is not a folder: it would be fetched, so it is not found. A
processor takes a few seconds for each thing heard (about a minute and a half for all of them); on
the card, ctranslate2 needs cuBLAS 12 on the `PATH` (`FACTOTUM_CUDA_DLL_DIR`, or the folder Ollama
keeps its own in, which the rig finds), and a card that something else is using is slower than the
processor. A real model hears a voice a little differently on each device, so what is looked for in
what it heard is the stem of a word and how much alike two sentences are, never a sentence.

The first runs found what the stand-ins had hidden: the PyAV that was locked could not decode a
single recording (`av` is held below 19), Whisper left to guess the language took a short command
for Arabic or Polish half of the time (`options.languages`), and told to hear Italian it wrote the
credits of a subtitle for a second of silence (what is not speech is now filtered out before it
listens). See [`model-gateway.md`](model-gateway.md#speech).

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

The tests of the terminal run against a real edge served by uvicorn on a loopback port, so they need
no Docker.

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
