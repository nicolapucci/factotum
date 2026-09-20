# 0008. The model gateway is the only place that knows models

Status: accepted, 2026-09-19. The data policy and the `local` or `remote` attribute of a binding
were superseded by [0010](0010-where-data-goes-is-the-users-choice-of-deployment-and-providers.md).

## Context

The user chooses the models: a local server or a hosted provider with the user's own key. Providers
differ in protocol, in how they do structured output and in what they refuse. Keys are sensitive and
addresses are user-controlled. The prototype hardcoded two models, used one model where another would
have fitted, and turned every error into `None`, which crashed later.

## Decision

- Services ask for a **role** (`chat.main`, `chat.fast`, `embedding`, `stt`, `tts`, `rerank`,
  `web_search`), never a model. A user or the tenant binds a role to a **binding**: provider kind,
  address, key, model name, options and `local` or `remote`.
- One adapter per provider kind (OpenAI, Anthropic, Gemini, Ollama, and a generic OpenAI-compatible
  one), written on `httpx` and pydantic, without vendor SDKs, so timeouts, retries and redaction are
  uniform and dependencies stay few.
- A normalized API: streamed chat events, structured output (a JSON schema in, a validated object
  out, with each provider's native mechanism or validation and retries) and typed errors, so callers
  react to the kind of error and not to a provider.
- Callers may ask for a **profile** of a role (maximum input tokens, streaming, structured output,
  locality) but never see a model name. The maximum input is declared by the user.
- Data policy: a collection is `local_only` or `external_allowed`, a binding `local` or `remote`, and
  the policy is enforced by the conversation service and again by the gateway.
- Addresses go through the SSRF guard. Keys are encrypted at rest, write-only, sent in headers only and
  redacted from every log.

## Consequences

- A new provider is one adapter class and its tests, based on the provider's current documentation.
- Callers cannot depend on model-specific behaviour.
- Ollama is called through its native API so the context window is set explicitly instead of relying
  on a default that may truncate the prompt silently.
