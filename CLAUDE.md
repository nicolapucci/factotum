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
uv run pytest e2e                      # the whole assistant in one process, no Docker (docs/development.md)
uv run pytest -m integration           # needs the Postgres of docker compose (FACTOTUM_POSTGRES_PASSWORD)
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live   # real Ollama and hosted providers, opt-in; see docs/model-gateway.md
# the knowledge service against real everything, and a book of your own (never copied or committed):
FACTOTUM_LIVE_TESTS=1 FACTOTUM_LIVE_BOOK=/path/to.pdf uv run pytest -m live services/knowledge/tests/live -s
# the conversation service against real models (see docs/conversation.md):
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/conversation/tests/live -s
# the actions service against real models (see docs/actions.md):
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/actions/tests/live -s
# skills that are only instructions, against real models (see docs/skills.md):
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/actions/tests/live -s -k live_instructions
# the tests that run scripts in real containers need the socket of an engine (see docs/runner.md):
uv run pytest -m integration services/runner/tests/test_runner_sandbox_engine.py
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
- The development file of devices, the development file of secrets, the development authentication,
  the development delegation of one service to another (`DevDelegation`, `DevPrincipalDelegation`),
  the enabling of a folder of skills by reading it and the runner itself refuse to be built outside
  development and tests, and the environment defaults to production. The hub replaces them
  (slice 7).
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
- **A script runs in a container of its own and reaches only what its grant lists** (ADR 0015). The
  `runner` is the only service given the socket of the engine (as much as being root on the
  machine), and a job cannot say what the container is like: the body of the container is built in
  one function from settings, and nothing about it comes from a job. A container has no network
  unless the skill was granted hosts, and then its only exit is the egress proxy with a grant signed
  by the runner; the proxy checks every address a name resolves to and connects to the one it
  checked. The key of the grants goes to the runner and the proxy and to nothing else, has no
  default, and both refuse to start without it. A skill is given only the secrets that it declared,
  that were granted and that are its owner's own; secrets are the environment of the container and
  nothing else, and are taken out of what it wrote. What a script prints is read strictly as
  `{message, data, commands}` and is never believed further than that: commands are data that go
  through `authorize()` first, and what a failure says is never what the script said. Do not add a
  field to a job that steers the container, do not give another service the socket, and do not put
  anything else on the sandbox network.
- **A plan is data, a model never runs anything, and a model never reads what a service
  answers** (ADR 0016). A skill that is only instructions is followed by a model that writes a plan
  once: steps that are requests or device commands, with placeholders for secrets, values and the
  answers of earlier requests. `factotum.actions.instructions.checks` decides whether the plan may be
  run (hosts with the same function as the egress proxy, a `read_only` skill only `GET`s, a secret
  only in a request, only the commands of the vocabulary, only what exists) *before* anything is
  sent, and again when a yes comes; the plan is written before the question and kept with it.
  `executor.py` is the only file that runs for such a skill: it is one file of the standard library,
  it shares its reading of a placeholder with the checks, and it runs in the sandbox with the
  network and the secrets that were granted (the actions service has no way out for a plan). A model
  is given the *names* of secrets and never a value, and never anything that came from a document or
  a page. Do not feed the answer of a request back to a model, do not add a step that runs code,
  and do not let a model choose a host that was not granted.
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
- `e2e` puts every service together in one process, with the real code on both sides of every
  boundary and only the hardware of a device and the models made up (and, as live tests, the real
  Whisper and Piper: docs/development.md). What crosses a service boundary
  is tested there once, as a person would meet it; a unit test of each side does not show that the two
  fit (the speech roles were broken for every binding written the documented way until one did).
- Test file names are unique across the whole repository (mypy checks them in one run).
- Markers: `integration` needs Docker; `live` runs real models or calls real providers, with the
  user's own keys or the models of the machine, and never runs in CI.

## Gotchas

- YAML 1.1 reads an unquoted `on` / `off` as a boolean; quote them in skill manifests.
- Python's `ipaddress` calls `::1` reserved; the URL guard checks loopback first.
- Write files with LF line endings; `.gitattributes` normalizes them anyway.
- Provider wire formats (`services/model_gateway/providers/`) come from the provider's own
  documentation and types, never from memory. An OpenAI-compatible server is not OpenAI: declare what
  differs on the binding rather than assuming.
- `pytest.raises(match=...)` takes a regular expression: escape the dots (ruff's RUF043 enforces it).
- What Windows hides, Linux shows: an event loop reports a reset at the end of a connection as an end
  on Windows and raises `ConnectionResetError` on Linux, so a test over real sockets that passes here
  proves nothing about it. Give the code sides that fail as Linux's do (see the tests of the egress
  proxy), and run the suite in a Linux container before calling a network change done.
- A `.env` in the repository belongs to the stack, not to the tests: a root fixture runs every test
  from an empty directory. A test that wants a settings file makes one and moves there.
- Docker refuses to put files into a container whose root filesystem is read-only, so the sandbox
  does not use the flag (docs/runner.md says what it does instead); do not "fix" it back.
- Compose interpolates every service of the file even when its profile is off, so a required
  variable (`${X:?}`) stops a plain `docker compose up` for the database: give a key an empty default
  and let the service that needs it refuse to start.
- The grammar that Ollama makes of a JSON Schema (llama.cpp) fails for a `maxLength` of exactly
  2000 (1999 and 2001 are taken): the schema of a plan that a model is asked to answer with has no
  limits of size in it, and they are checked when the answer is read. A model that may leave a field
  out sometimes leaves out the address of a request, and one that is shown a field that does not
  apply fills it in: the schema is written strictly (`factotum.actions.instructions.writer`).
- **A schema that says nothing is an object to that grammar.** `{}` ("a value of any kind") makes
  the model write an object whatever it is asked for (a text came out as `{"hello world": ""}`, a
  number as `{"value": 42}`): a field that may be a text says `anyOf` its types. It cost a whole
  class of skills (every plain-text body was written as JSON) before a test with skills that nothing
  was tuned on showed it; the tests now refuse a part of the shape that has no `type` or `anyOf`.
- Tests import `--import-mode=importlib`: one test module cannot import another, so what tests share
  lives in a `testing.py` in the package (`factotum.edge.testing`, `factotum.terminal.testing`).
- Starlette's `TestClient` is unusable here (its deprecation warning is an error): what needs a
  WebSocket runs on a real one, with uvicorn (`ws="websockets-sansio"`, the implementation that is
  not deprecated) and the `websockets` client, as `factotum.edge.testing.serving` does.
- A test that measures a real model against `ActionsRig` gives it `patience_s`: the defaults give up
  on a model after a few seconds, which on a graphics card that something else is using is not a
  wrong answer but a slow one.
- `make_model_binding` gives every binding an address (a local Ollama). The two local speech providers
  have none, and a person writes their binding without one (`base_url=None`): a test of speech must too.
  The first ones did not, and a `KeyError` for the address went unseen until an end-to-end test.
- Whisper left to guess the language of a short command guesses wrong about half the time (Arabic for
  "apri il garage", Polish for "no, grazie"): bind `stt` with `options.languages`. And faster-whisper
  cannot read a recording with PyAV 19 (it passes `av.open` an argument that was removed), so `av` is
  held below 19. Told to hear Italian, Whisper writes the credits of a subtitle for a second of
  silence ("Sottotitoli e revisione a cura di QTSS"), so what is not speech is filtered out first. A
  stand-in engine showed none of this: it took a real model, so a change to the speech path has to be
  tried with one (`e2e/tests/test_e2e_speech.py`), not only with the stand-ins.
- The keys of `extra=` in a log call must not be attributes of a `LogRecord` (`name`, `message`,
  `args`...): the standard library raises `KeyError`, and only when the level is enabled, which is
  how a session died the first time the log was at INFO.
