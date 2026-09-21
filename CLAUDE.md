# Working agreements for this repository

Factotum is a self-hosted voice assistant. Read `docs/design.md` first, and `docs/decisions/` for the
reasons behind the big decisions.

## How to work

- Talk to the maintainer in Italian. Write code, comments, documentation and commit messages in
  English.
- Before building a new service or a large feature, say what will be built and how, and wait for the
  go-ahead. Work in the slices of the roadmap in `docs/design.md`, and stop at the end of each slice
  with a summary (what exists, how to try it, which tests ran).
- **Every change ships with unit tests, written together with the code.** A change is not finished
  until `ruff`, `mypy` and `pytest` all pass.
- The previous prototype (`../Butler`) is reference material only: never modify it.
- Commit small, conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`) on `main`, one per
  logical step, with the tests green. Do not push: the maintainer connects the remote.

## Commands

```bash
uv sync --all-packages                 # install everything from the lockfile
uv run ruff format && uv run ruff check
uv run mypy
uv run pytest                          # unit tests only need no Docker
uv run pytest -m integration           # needs the Postgres of docker compose (FACTOTUM_POSTGRES_PASSWORD)
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live   # real Ollama and hosted providers, opt-in; see docs/model-gateway.md
# the knowledge service against real everything, and a book of your own (never copied or committed):
FACTOTUM_LIVE_TESTS=1 FACTOTUM_LIVE_BOOK=/path/to.pdf uv run pytest -m live services/knowledge/tests/live -s
# the conversation service against real models (see docs/conversation.md):
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/conversation/tests/live -s
# the actions service against real models (see docs/actions.md):
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/actions/tests/live -s
# the edge, and a device in a terminal (docs/edge.md; needs config/devices.dev.yaml and the services):
uv run factotum-terminal --profile smart-display
FACTOTUM_POSTGRES_PASSWORD=dev docker compose up -d --wait                       # the database
FACTOTUM_POSTGRES_PASSWORD=dev docker compose --profile services up -d --build   # and the services
```

On Windows `uv` was installed with winget; a shell started before that may not have it on its PATH.

## Design invariants (change them only with a new decision record)

- Services are model-agnostic: only `model-gateway` knows providers, addresses and keys. Everything
  else asks for a role.
- Where data goes is decided by the deployment and the providers the user bound, not by a policy
  in the app (ADR 0010): no `local_only`, no data classification on requests or collections, no
  locality on bindings. Do not reintroduce them. The only network rule is which networks the
  gateway may reach, and it follows `FACTOTUM_TENANT_MODE`.
- Permissions never depend on a voice. There is no speaker recognition. `authorize()` is a pure
  function of the principal, the device policy and the action.
- `tenant_id` comes from the authenticated token, never from a request body. Tenant-owned data is
  scoped by tenant, and tests use two tenants.
- Administration goes through authenticated hub sessions, never through a device.
- Approvals fail closed: required but not configured means refused.
- Secrets never reach a log, an error message or an audit record: use `factotum.common.redaction`.
- A passage retrieved from a document is somebody else's words: it is `untrusted`, data and never an
  instruction (ADR 0011). What reads documents runs in an isolated process and its answer is
  validated like any other input. A collection somebody may not see is *not found*, never forbidden.
- A turn of the conversation carries no history: what was said is looked up only when a message
  refers to it, and what an answer quoted from a document stays marked as somebody else's words
  (ADR 0012). A shared device has no history of its own, only the last minute of what was said on it.
  A chunk of streamed text is never trimmed: the space at its edge is a word break.
- The conversation service knows no device: it streams text and says what it cited. What a device is
  sent of an answer is the edge's choice, by what the device declared it can render, and a part it
  cannot render is never sent to it (ADR 0013). A connection answers one utterance at a time and a
  new one interrupts the answer to the last; the last connection of a device wins. What a device is
  and whom it acts for come from the registry, never from what it says.
- The development file of devices, the development authentication, the development delegation of
  one service to another (`DevDelegation`, `DevPrincipalDelegation`) and the enabling of a folder of
  skills by reading it refuse to be built outside development and tests, and the environment
  defaults to production. The hub replaces them (slice 7).
- **The model proposes, the code decides** (ADR 0014). What plans an action reads the message alone:
  never a document, a page of the web or the conversation. What a model says (the skill, the device,
  the values, the questions) is checked in code before anything with an effect is decided, and what
  fails is *nothing to do* or *needs more*, never a guess. A version of a skill never changes; only
  an enabled one runs, and enabling it grants exactly what it declares. A spoken yes is a
  confirmation, not an approval and not authentication: whatever is not clearly a yes is not one, it
  is taken once, and the action is decided again when it comes. Every step of an action is audited,
  and a record never says that something ran without the confirmation or the approval its decision
  required.
- The conversation service asks the actions service about every message and does not depend on it:
  with no URL nothing is asked, and when it cannot be reached the message is a question and the turn
  says so in a warning. Skills, the audit trail and everything that manages them are never served
  through a device.
- Every outbound URL the user controls goes through `factotum.common.urlguard`.
- Interfaces between services are pydantic models in `factotum.contracts`, immutable and strict
  about unknown fields (`Contract`). The command vocabulary is defined once and everything else
  derives from it.
- Timestamps are timezone-aware (ruff's `DTZ` rules enforce it). Errors end in `Error`.

## Testing rules

- Unit tests never touch the network: `pytest-socket` allows loopback only. Warnings are errors.
- Prefer fakes that implement the real interface over mocks. Rules that must hold for every input are
  also tested as properties with Hypothesis.
- Wherever data is scoped, test with two tenants.
- Test file names are unique across the whole repository (mypy checks them in one run).
- Markers: `integration` needs Docker; `live` calls real providers with the user's own keys and never
  runs in CI.

## Gotchas

- YAML 1.1 reads an unquoted `on` / `off` as a boolean; quote them in skill manifests.
- Python's `ipaddress` calls `::1` reserved; the URL guard checks loopback first.
- Write files with LF line endings; `.gitattributes` normalizes them anyway.
- Provider wire formats (`services/model_gateway/providers/`) come from the provider's own
  documentation and types, never from memory. An OpenAI-compatible server is not OpenAI: declare what
  differs on the binding rather than assuming.
- `pytest.raises(match=...)` takes a regular expression: escape the dots (ruff's RUF043 enforces it).
- Tests import `--import-mode=importlib`: one test module cannot import another, so what tests share
  lives in a `testing.py` in the package (`factotum.edge.testing`, `factotum.terminal.testing`).
- Starlette's `TestClient` is unusable here (its deprecation warning is an error): what needs a
  WebSocket runs on a real one, with uvicorn (`ws="websockets-sansio"`, the implementation that is
  not deprecated) and the `websockets` client, as `factotum.edge.testing.serving` does.
- A test that measures a real model against `ActionsRig` gives it `patience_s`: the defaults give up
  on a model after a few seconds, which on a graphics card that something else is using is not a
  wrong answer but a slow one.
- The keys of `extra=` in a log call must not be attributes of a `LogRecord` (`name`, `message`,
  `args`...): the standard library raises `KeyError`, and only when the level is enabled, which is
  how a session died the first time the log was at INFO.
