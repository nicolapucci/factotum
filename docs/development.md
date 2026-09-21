# Development

## Setup

You need Git, Docker, and [uv](https://docs.astral.sh/uv/) (on Windows: `winget install
astral-sh.uv`). uv finds a Python 3.13 or downloads one.

```bash
uv sync --all-packages      # creates .venv from uv.lock
cp .env.example .env        # then set FACTOTUM_POSTGRES_PASSWORD
```

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
libs/common/      authorization, redaction and logging, settings, the SSRF guard, development auth
libs/devices/     the SystemDevice interface and a fake for tests
libs/web/         what the HTTP interfaces share: errors, authentication, body limits
libs/db/          the Postgres pool, migrations and the harness for tests against a real database
libs/model_client/ how a service reaches the model gateway: chat, streamed chat, embeddings, profiles
services/         one folder per service (so far: model_gateway, knowledge, conversation, edge)
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

## Database

```bash
FACTOTUM_POSTGRES_PASSWORD=dev docker compose up -d --wait
docker compose exec postgres psql -U factotum -d factotum
docker compose down -v      # also removes the data volume
```

The database listens on `127.0.0.1:${FACTOTUM_POSTGRES_PORT:-5432}` only. The pgvector extension is
created the first time the database starts (`infra/postgres/initdb/`).
