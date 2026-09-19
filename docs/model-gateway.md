# The model gateway

The only service that knows providers, addresses and keys (ADR 0008). Everything else asks for a
*role*; the gateway turns "the chat model of this user" into an HTTP call to whatever the user
chose, and gives the answer back in one shape whichever provider it came from.

*Implemented*: chat (whole or streamed), structured output and embeddings, through OpenAI,
Anthropic, Gemini, Ollama and any OpenAI-compatible server. The roles `stt`, `tts`, `rerank` and
`web_search` exist in the contracts and are added with the slices that need them.

## Roles, bindings and profiles

A **role** says what a service needs a model for: `chat.main` (writes answers), `chat.fast`
(routing and extraction), `embedding`. A **binding** says which model serves a role: provider,
model, address, where the key is, whether the model is local, and a few options. A user binds roles
for themselves, the tenant for everybody.

Which binding serves a request:

1. A binding *for the role itself* wins wherever it is: the user's own, else the tenant's.
2. Only if nobody bound `chat.fast` does `chat.main` serve it (a user who bound just their main
   model does not shadow a fast model the tenant chose).
3. A device that acts for the tenant, such as a speaker in the living room, is served by the
   tenant's bindings only.
4. No binding is a `binding_not_found` error that names the role. There is no fallback across
   tenants, and none for `embedding`.

A **profile** says what a role can do without saying which model does it: maximum input tokens,
maximum output tokens, streaming, structured output (`native` or `emulated`), locality. Callers use
it to size their prompts. `GET /v1/profiles/{role}`.

Binding options (all optional, all with prudent defaults):

| Option | Meaning |
| --- | --- |
| `max_input_tokens` (8192) | What the caller may put in a prompt. **Declared by you**: model catalogues change too often to be hardcoded. |
| `max_output_tokens` | A cap on answers; a caller's own limit is held under it. |
| `temperature` | Used when the caller does not set one. Otherwise the provider's default. |
| `timeout_s` (60) | How long to wait for the provider; connecting gets at most 10 s of it. |
| `native_structured_output` | `false` for a model that lacks the feature, `true` for a compatible server that has it. |
| `num_ctx` | Ollama: the context window, prompt and answer together. |
| `dimensions` | Embeddings: the vector size to ask for (where the provider can) and to expect. |
| `stream_usage` | OpenAI-compatible servers: ask for token usage in streams. |

## Configuring bindings

Until the hub manages them (slice 7), bindings come from a YAML file for the development tenant:
`FACTOTUM_GATEWAY_BINDINGS_FILE=config/bindings.dev.yaml`. Start from
[`config/bindings.example.yaml`](../config/bindings.example.yaml), which is tested to be valid.
**A binding never contains a key**: `secret_ref: env:OPENAI_API_KEY` names an environment variable,
and the variable holds the key. A key pasted where a reference belongs is refused, and the error
does not repeat it.

```yaml
bindings:
  - role: chat.main
    provider: ollama
    model: llama3.1:8b-instruct-q4_K_M
    base_url: http://localhost:11434
    options: {max_input_tokens: 8192}
  - role: embedding
    provider: openai
    model: text-embedding-3-small
    secret_ref: env:OPENAI_API_KEY
    options: {dimensions: 512}
```

## What each provider needs, and what the adapter absorbs

Every adapter is written directly on `httpx` and pydantic, from the provider's own documentation
and types. The same normalized request goes to all of them (`tests/test_adapter_contract.py` proves
it with each provider's real wire format).

| | Address | Key | Absorbed differences |
| --- | --- | --- | --- |
| `openai` | `https://api.openai.com/v1` | `Authorization: Bearer` | `max_completion_tokens`; structured output native (`json_schema`, `strict` when the schema fits); usage requested in streams; a refusal is a content-filter finish; an exhausted quota (a 429) is not retried |
| `openai_compatible` | yours, **`/v1` included** | Bearer, if any | `max_tokens`; structured output *emulated* unless you declare it native (many servers accept a schema and ignore it); usage in streams only if `stream_usage`; context overflow recognized in vLLM and llama.cpp too |
| `ollama` | yours | none (Bearer for a proxy or the hosted service) | Native API. **The context window is always sent**: `num_ctx`, else `max_input_tokens` + `max_output_tokens` (or 2048). It depends on the binding only, because Ollama reloads the model when it changes. Embedding inputs that do not fit are an error, never silently shortened |
| `anthropic` | `https://api.anthropic.com` | `x-api-key` and `anthropic-version` | System messages lifted out of the conversation; `max_tokens` (required) defaults to 4096; temperature held at 1.0; stream errors typed by their event; **no embeddings** (`role_not_supported`) |
| `gemini` | `https://generativelanguage.googleapis.com` | `x-goog-api-key`, **never in the URL** | Roles `user`/`model`; system messages become the system instruction; a prompt the API refuses is `content_filtered`, not an empty answer; thinking tokens counted as output; the `RetryInfo` delay of a rate limit honored; embedding batches split at 100 |

The measured reason for the Ollama rule: with its default window (2048 tokens on the version tested,
0.34), a prompt of 10 450 tokens is cut to 2050 and the model answers that there is no secret word
in a text that starts by telling it the secret word. With the window the gateway asks for, it
answers correctly. `tests/live/test_live_ollama.py` keeps a smaller version of that test.

## Structured output

A request with a `response_schema` (JSON Schema, checked first: an invalid one is `invalid_schema`)
gets back `parsed`, a value that has been **validated against the schema**, never just text that
looks like JSON.

- Where the provider can constrain the decoding to a schema (OpenAI, Ollama, Anthropic, Gemini),
  the schema is handed to it, if it is within the subset of JSON Schema the provider is known to
  enforce. A schema outside the subset is asked for in the prompt instead of being sent to be
  refused. Where the provider cannot constrain the decoding, or the binding says its model cannot,
  the gateway asks for JSON in the system message.
- Either way the answer is parsed (a code fence or a sentence around the JSON is tolerated) and
  validated. An invalid answer goes back to the model with the problems found, up to two times.
- An answer cut off by the output limit, or refused, is not retried: asking again would not help.
  The last failure is `invalid_structured_output`, with the attempts and the problems in `details`.
- Token usage is added up over the attempts.

## Streaming

`stream: true` gives server-sent events, one JSON object per event:
`text_delta`, then `usage` if the provider reports it, then `finish` as the last event. Or, if the
provider fails after the start, a final `error` event with the code of the matching error.

A failure **before the first event** is an ordinary HTTP error with its own status and JSON body:
the gateway waits for the first event before answering 200, so a stream never starts with a status
that the body contradicts. Only that part is retried; a stream that has started is never replayed.
When the client goes away the provider stream is closed, so the answer stops costing tokens.

## Retries

Each provider call is tried up to 3 times, with exponential backoff and jitter (0.5 s, growing,
never more than 8 s). A provider's `Retry-After` is honored when it is short; when it is longer than
8 s the error is raised at once, because a voice assistant cannot sit silent for a minute. Retried:
`rate_limited`, `provider_unavailable`, `provider_timeout`. Never retried: a refused key, an invalid
request, a prompt that is too long, a refusal.

## Data locality

A request says what its data may be sent to (`data_classification`: `any` or `local_only`). Before
any network access, `local_only` data is refused by a model that is not local: `policy_violation`,
403. This is the second line of defence: the conversation service does not put such content in the
request in the first place.

Whether a model is local is what the user declares (`locality`), otherwise:

- for `ollama` and `openai_compatible`, judged by the address: loopback, private ranges,
  `host.docker.internal`, `.local`, `.lan`, `.internal` and bare names are local; the rest is remote;
- for the hosted providers, **always remote**: a proxy on the LAN in front of OpenAI still sends the
  data to OpenAI.

An unknown address is remote: calling a local server remote costs some convenience, the opposite
would leak documents.

The HTTP client is the last wall. A local model may be on the private network, a remote one only on
the public internet; cloud metadata addresses are never reachable; every resolved address is
checked and the connection goes to the address that was checked; redirects are not followed.

## Secrets and logs

- Keys live in the environment and in the gateway's memory (`SecretStr`, never in a `repr`), and go
  in a header, never in a URL or a body.
- Provider error messages are redacted and cut to 500 characters, because providers sometimes quote
  the key they were given. Only the type of a transport error is reported, since its message may
  quote the address.
- One log line per call: role, tenant, provider, model, locality, outcome, duration, token counts.
  **Never the conversation.** A test checks that neither the prompt nor the key reaches the log.
- A request that breaks the contract gets a 422 that says where and why, never what: the value could
  be the text of a conversation or a key.

## The HTTP API

For the other services, never for the outside world. Authentication is the development
authenticator for now (`x-factotum-dev-subject: user|tenant`), so **the gateway refuses to start
outside `FACTOTUM_ENVIRONMENT=development` or `test`** until the hub issues real credentials.

| Route | Body → answer |
| --- | --- |
| `POST /v1/chat` | `ChatRequest` → `ChatResult`, or server-sent events with `stream: true` |
| `POST /v1/embeddings` | `EmbeddingRequest` → `EmbeddingResult` (`vectors`, `dimensions`, `space_id`, `usage`) |
| `GET /v1/profiles/{role}` | → `Profile` |
| `GET /healthz`, `GET /readyz` | → `{"status": "ok"}` |

The `space_id` names the space the vectors live in (provider, model and dimensions, as an opaque
id): vectors of different spaces must never be compared. The gateway checks that there is one
vector per input, that they agree on a size, and that it is the size the binding declares.

Errors are always `{"error": {"code", "message", "details"}}`. The code is stable; the status says,
roughly, whose fault it is:

| Status | Codes |
| --- | --- |
| 401 | `auth_error` |
| 403 | `policy_violation` |
| 404 | `binding_not_found` |
| 413 | `context_too_long`, `payload_too_large` (bodies over 8 MiB) |
| 422 | `invalid_request_body`, `invalid_schema`, `invalid_request`, `role_not_supported`, `content_filtered` |
| 429 | `rate_limited`, with `Retry-After` when the provider said how long |
| 502 | `provider_unavailable`, `provider_auth_error`, `invalid_structured_output`, `secret_not_found`, `url_not_allowed`, `provider_error` |
| 504 | `provider_timeout` |
| 500 | `internal_error` (nothing about it is revealed) |

## Running it

```bash
cp config/bindings.example.yaml config/bindings.dev.yaml     # then adjust it
FACTOTUM_ENVIRONMENT=development FACTOTUM_GATEWAY_BINDINGS_FILE=config/bindings.dev.yaml \
  uv run python -m factotum.model_gateway
curl -s localhost:8081/v1/profiles/chat.main
```

In a container (`FACTOTUM_ENVIRONMENT=development` in `.env`; a model on the host is
`http://host.docker.internal:11434`):

```bash
docker compose --profile services up -d --build --wait model-gateway
```

The image is built from the repository root with only this service and what it needs, runs as a
non-root user and has a health check. The port is published on loopback only.

## Tests

- Unit tests for every module, at 100% of statements and branches for the package. Adapters are
  tested against each provider's real wire format with `httpx.MockTransport`.
- `tests/test_adapter_contract.py`: one suite, run against every adapter, for what must not differ.
- `tests/live/`: the gateway against real providers (opt-in, never in CI). Against a real Ollama:

```bash
ollama serve   # and the models named in the file pulled
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/model_gateway/tests/live -k ollama
```

  Against the hosted providers, with your own keys in the environment (a provider without a key is
  skipped, and each test costs a few tokens):

```bash
export OPENAI_API_KEY=... ANTHROPIC_API_KEY=... GEMINI_API_KEY=...
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/model_gateway/tests/live -k hosted
```

  One hosted test needs no key: it sends a key that cannot be valid and checks that OpenAI, Anthropic
  and Gemini each answer with something the gateway maps to the same `provider_auth_error`. Gemini
  reports a bad key as a 400 with `API key not valid`, and it is recognized as what it is.

## Adding a provider

1. A module in `providers/` with a class that satisfies `ProviderAdapter`: `kind`,
   `native_structured_output`, `chat`, `chat_stream` and `embed`. Use the plumbing of
   `providers/base.py` (typed errors from statuses, redaction, the two stream readers, retry-after).
   Read the provider's own documentation and types: write the wire models in the adapter.
2. Register it in `default_adapters()` in `core.py`, and its default address if it has one.
3. Add its wire format to `tests/test_adapter_contract.py`, and its own file of tests for what only
   it does.

## Known limits

- The gateway does not count tokens: the provider says when a prompt does not fit
  (`context_too_long`), and callers budget with the profile.
- Each call builds an HTTP client; pooling per binding will come when latency asks for it.
- Asymmetric embedding models (a different prefix or task for a query and for a document) are not
  modelled yet; the knowledge service will decide how.
- Keys come only from the environment. An encrypted store arrives with the hub.
