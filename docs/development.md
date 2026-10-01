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
