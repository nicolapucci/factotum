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
uv run pytest -m integration           # needs Docker services (none yet)
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live   # real Ollama and hosted providers, opt-in; see docs/model-gateway.md
FACTOTUM_POSTGRES_PASSWORD=dev docker compose up -d --wait                       # the database
FACTOTUM_POSTGRES_PASSWORD=dev docker compose --profile services up -d --build   # and the services
```

On Windows `uv` was installed with winget; a shell started before that may not have it on its PATH.

## Design invariants (change them only with a new decision record)

- Services are model-agnostic: only `model-gateway` knows providers, addresses and keys. Everything
  else asks for a role.
- Permissions never depend on a voice. There is no speaker recognition. `authorize()` is a pure
  function of the principal, the device policy and the action.
- `tenant_id` comes from the authenticated token, never from a request body. Tenant-owned data is
  scoped by tenant, and tests use two tenants.
- Administration goes through authenticated hub sessions, never through a device.
- Approvals fail closed: required but not configured means refused.
- Secrets never reach a log, an error message or an audit record: use `factotum.common.redaction`.
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
