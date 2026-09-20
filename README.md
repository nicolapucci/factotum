# Factotum

A self-hosted voice assistant that answers from **your own documents** and from the **web**, performs
**actions** on your devices, and runs on **the models you choose**.

> Status: foundations (Slice 0), the model gateway (Slice 1) and the knowledge service (Slice 2)
> are done. The gateway talks to OpenAI, Anthropic, Gemini, Ollama and any OpenAI-compatible server
> behind one API; the knowledge service reads your documents (text, Markdown, HTML, PDF, Word) and
> answers with the passages that answer a question, by meaning and by words. The other services are
> not built yet. See the roadmap in [`docs/design.md`](docs/design.md).

## Principles

- **You choose the models.** Every model (chat, embeddings, speech-to-text, text-to-speech) and the web
  search backend is provided by the user: a local server, or a hosted provider with the user's own
  key. No service in the system knows or hardcodes which model it is talking to.
- **You decide where your data goes, by choosing where to run it and which models to use.** On
  premise with a local model such as Ollama, nothing leaves your network; with a hosted provider,
  what you send goes there. The system does not ask you to declare that again and does not
  police it.
- **You teach it what to do.** An action is a skill: a markdown document with optional code, with the
  permissions it needs declared up front and run in isolation.
- **Permissions never depend on a voice.** They come from whom a device acts for and from the device's
  and the action's own properties.
- **Tested by default.** Every piece of code ships with unit tests.

## Where things are

| | |
| --- | --- |
| [`docs/design.md`](docs/design.md) | The design: architecture, identity and permissions, devices, skills, models, roadmap |
| [`docs/model-gateway.md`](docs/model-gateway.md) | The model gateway: bindings, providers, structured output, streaming, API |
| [`docs/knowledge.md`](docs/knowledge.md) | The knowledge service: from a file to a passage, hybrid search, ownership, API, what was measured |
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
