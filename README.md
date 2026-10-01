# Factotum

A self-hosted voice assistant that answers from **your own documents** and from the **web**, performs
**actions** on your devices, and runs on **the models you choose**.

> Status: foundations (Slice 0), the model gateway (Slice 1), the knowledge service (Slice 2), the
> conversation service (Slice 3), the edge with a terminal client (Slice 4), actions (Slice 5a),
> the runner that runs them (Slice 5b), skills that are only instructions (Slice 6), and the hub's
> tenant, its users, the tokens every service checks, a device's pairing, the external devices a
> skill is bound to, and the models bound to a role (Slices 7a-7c) are done. The gateway talks to
> OpenAI, Anthropic, Gemini, Ollama and any
> OpenAI-compatible server behind one API; the knowledge service reads your documents (text,
> Markdown, HTML, PDF, Word) and answers with the passages that answer a question, by meaning and by
> words; the conversation service answers a message from them, streamed, without carrying the
> conversation along: what was said is looked up only when a message refers to it; the edge is where
> devices connect over a WebSocket, and hands each the answer in the pieces it can render, a
> sentence at a time; the actions service works out whether a message asks for something to be done,
> which skill does it and whether this device may have it done, asks for a yes when it must, runs the
> skill (its script in a container of its own, that reaches only the hosts it was granted; or, for a
> skill that is only words, a plan that a model writes and that code checks before the same container
> runs it) and has a device do what it answers with, and keeps a trail of it. Skill secrets and linked
> accounts, voice, and the web app are not built yet. See
> the roadmap in [`docs/design.md`](docs/design.md).

## Principles

- **You choose the models.** Every model (chat, embeddings, speech-to-text, text-to-speech) and the web
  search backend is provided by the user: a local server, or a hosted provider with the user's own
  key. No service in the system knows or hardcodes which model it is talking to.
- **You decide where your data goes, by choosing where to run it and which models to use.** On
  premise with a local model such as Ollama, nothing leaves your network; with a hosted provider,
  what you send goes there. The system does not ask you to declare that again and does not
  police it.
- **You teach it what to do.** An action is a skill: a markdown document with optional code, with the
  permissions it needs declared up front and run in isolation. A skill can be only words: a model
  turns them into a plan, and code decides whether the plan may be run.
- **Permissions never depend on a voice.** They come from whom a device acts for and from the device's
  and the action's own properties.
- **Tested by default.** Every piece of code ships with unit tests.

## Where things are

| | |
| --- | --- |
| [`docs/design.md`](docs/design.md) | The design: architecture, identity and permissions, devices, skills, models, roadmap |
| [`docs/model-gateway.md`](docs/model-gateway.md) | The model gateway: bindings, providers, structured output, streaming, API |
| [`docs/knowledge.md`](docs/knowledge.md) | The knowledge service: from a file to a passage, hybrid search, ownership, API, what was measured |
| [`docs/conversation.md`](docs/conversation.md) | The conversation service: a turn with no history, the log searched when a message refers to it, API, what was measured |
| [`docs/edge.md`](docs/edge.md) | The edge: the protocol of the devices, what each is sent, commands, the terminal client, API, what was measured |
| [`docs/actions.md`](docs/actions.md) | The actions service: skills, choosing one, deciding, confirmations and approvals, running them, the audit trail, API, what was measured |
| [`docs/skills.md`](docs/skills.md) | Writing skills: the `SKILL.md`, scripts, and instructions that a model turns into a plan (the language of a plan, what is checked, what a small model does with it) |
| [`docs/runner.md`](docs/runner.md) | The runner and the egress proxy: a container for each run, the grant that says where it may go, secrets, how it is deployed and tested, what was measured |
| [`docs/hub.md`](docs/hub.md) | The hub: a tenant and its administrator, logging in, the tokens every service checks, API |
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
