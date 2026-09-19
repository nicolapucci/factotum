# Factotum

A self-hosted voice assistant that answers from **your own documents** and from the **web**, performs
**actions** on your devices, and runs on **the models you choose**.

> Status: foundations done (Slice 0). The shared contracts, the authorization rules and the tooling
> exist and are tested; the services are not built yet. See the roadmap in
> [`docs/design.md`](docs/design.md).

## Principles

- **You choose the models.** Every model (chat, embeddings, speech-to-text, text-to-speech) and the web
  search backend is provided by the user: a local server, or a hosted provider with the user's own
  key. No service in the system knows or hardcodes which model it is talking to.
- **You control your documents.** Each collection carries a data policy that decides whether its
  content may leave the machine. The system enforces it; it is not left to convention.
- **You teach it what to do.** An action is a skill: a markdown document with optional code, with the
  permissions it needs declared up front and run in isolation.
- **Permissions never depend on a voice.** They come from whom a device acts for and from the device's
  and the action's own properties.
- **Tested by default.** Every piece of code ships with unit tests.

## Where things are

| | |
| --- | --- |
| [`docs/design.md`](docs/design.md) | The design: architecture, identity and permissions, devices, skills, models, roadmap |
| [`docs/decisions/`](docs/decisions/README.md) | Why the big decisions were taken |
| [`docs/development.md`](docs/development.md) | Setup, the checks, layout, conventions |
| [`CLAUDE.md`](CLAUDE.md) | Working agreements for AI-assisted sessions |

## Quick start

```bash
uv sync --all-packages
uv run pytest
```

## License

Not chosen yet.
