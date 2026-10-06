# The model gateway

The only service that knows providers, addresses and keys (ADR 0008). Everything else asks for a
*role*; the gateway turns "the chat model of this user" into an HTTP call to whatever the user
chose, and gives the answer back in one shape whichever provider it came from.

*Implemented*: chat (whole or streamed), structured output and embeddings, through OpenAI,
Anthropic, Gemini, Ollama and any OpenAI-compatible server; speech-to-text and text-to-speech
(`stt`, `tts`), through two providers that run in the gateway's own process (below). The roles
`rerank` and `web_search` exist in the contracts and are added with the slices that need them.

## Roles, bindings and profiles

A **role** says what a service needs a model for: `chat.main` (writes answers), `chat.fast`
(routing and extraction), `embedding`. A **binding** says which model serves a role: provider,
model, address, where the key is, and a few options. A user binds roles
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
maximum output tokens, streaming, structured output (`native` or `emulated`). Callers use
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

In production, bindings are the hub's ([`hub.md`](hub.md)): `POST /v1/models` binds a role to a
model, for the tenant or for the caller's own use, and `FACTOTUM_GATEWAY_HUB_URL` is where the
gateway fetches the tenant's bindings from, once, in its own lifespan (`HubBindingRepository`), the
same way it is built for devices in [`edge.md`](edge.md). **Without the hub this is a YAML file, for
development**: `FACTOTUM_GATEWAY_BINDINGS_FILE=config/bindings.dev.yaml`. Start from
[`config/bindings.example.yaml`](../config/bindings.example.yaml), which is tested to be valid.

**A binding never contains a key, whichever source it comes from**: `secret_ref: env:OPENAI_API_KEY`
names an environment variable, and the variable holds the key, on the machine that runs the gateway.
The hub only ever holds this reference, never the key itself — pasting one where a reference belongs
is refused, and the error does not repeat it.

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

**Two roles on one Ollama model need the same window.** Ollama keeps a model loaded for one window
at a time, so `chat.main` and `chat.fast` bound to the same model with different output limits (their
windows are `max_input_tokens` + `max_output_tokens`) load it again each time the other role asks: a
conversation turn that took 0.9 s took 14 s, found by the live test of the conversation service.
Give both bindings the same `num_ctx`, or the same limits.

## Speech

`stt` and `tts` are served by two providers that run in the gateway's own process, not over HTTP:
no address, no key, so a binding needs neither `base_url` nor `secret_ref`. `model` is a Whisper
model size (`whisper_local`: `tiny`, `base`, `small`...) or a Piper voice name (`piper_local`:
`it_IT-paola-medium`...), or a path to one already on disk. Chat, streaming and embeddings are not
what either is for: asking one for them is `role_not_supported`, the same as Anthropic and
embeddings. What a voice says may be asked for whole or as a stream (below).

```yaml
bindings:
  - role: stt
    provider: whisper_local
    model: small
    options:
      languages: [it]
  - role: tts
    provider: piper_local
    model: it_IT-paola-medium
```

`options.languages` says which languages are spoken to the device, as the codes Whisper uses, and
**is Italian when the binding does not say**. Left to guess among all of its own, Whisper often picks
one that nobody spoke for a short command: with the Italian voice of Piper speaking, 7 of 15
commands and answers came out in Spanish, Arabic, Polish or Persian ("apri il garage" as «أبيل
غراف»), which nothing downstream can route. One language is forced; several limit the guess to
them, at the cost of one more pass of the model over the recording to see which of them it hears
best; `languages: null`, written out, leaves the guess to the model. A household that speaks
English too binds `[it, en]`.

**The size is what is heard against how long it takes.** The same 25 commands in Italian, said by
the voice of Piper's and heard on a processor, were heard as they were said by 15 with `small`, 16
with `medium` and 20 with `large-v3` (a minute and forty seconds, three and a half and six, for all
of them), and `small` heard "alza il volume a 70" as "a 60" (the measurement and what it did not
settle, the words of English in a command, are in
[`development.md`](development.md#a-series-of-commands-said-in-italian)). The example binding keeps
`small` because it is the one that fits everywhere; bind a bigger size where there is room for it.

**A vocabulary is for the words that Whisper does not know how to write.** `options.vocabulary` lists
them: the names of what is in the home, and the words of another language that are said in every one
(`garage`, `router`). The adapter gives them to the model before it listens, as a list the way a
person writes one (`Garage, router, wifi.`), which is faster-whisper's `initial_prompt`: Whisper takes
what it is given as what was said just before, and writes those words as they are more often. **It
is off unless the binding lists words**, so what is bound today is heard as it was, because what a
model is given it may say when nobody said it, and so it is a list and not a sentence (a sentence was
said back by `large-v3` for a lone "sì"). At most a hundred words, of at most 64 characters and 600
in all (a model reads about the last 220 tokens of what it is given), each once.

```yaml
  - role: stt
    provider: whisper_local
    model: medium
    options:
      languages: [it]
      vocabulary: [garage, router, wifi, lampada, salotto, camera da letto, cucina, volume, meteo,
                   Home Assistant]
```

What it does, on the 26 Italian commands of the series (the voice of Piper's, heard on a processor;
[`development.md`](development.md#a-series-of-commands-said-in-italian) has the table): `medium`
heard 21 as they were said with the ten words above instead of 17, and `large-v3` 23 instead of 21.
`router` stopped being `looter` and `Luther`, and "apri il garage", which every size mishears with
this voice, went from "A pigara!" to "Apri gara." with `medium` and was still wrong with `large-v3`
("A Pilgara", then "Appilgara"): a vocabulary helps a model that does not know a word, and does not
make a voice that it cannot follow clearer. What it costs: on the engine itself (a quick
measurement, not part of the series), `large-v3` heard a lone "sì" as "Sì, va bene." with the list,
and as a sentence of the home with a prompt that was a sentence (`medium` heard "Sì." both times),
which is the one thing here that a person would meet as something nobody said. So it is tried on
the models and the voice of the machine it is for, with the same series:

```bash
FACTOTUM_LIVE_TESTS=1 FACTOTUM_LIVE_VOCABULARY="garage,router,wifi,lampada,salotto" \
    uv run pytest e2e/tests/test_e2e_speech_series.py -s
```

Which words is the home's to say: the gateway knows no device and no skill, and a list that is
derived from the registry and the skills of a home is not built (see the open questions of
[`design.md`](design.md#13-open-questions)).

### A voice as a stream

`POST /v1/synthesize` with `stream: true` answers in server-sent events, as `/v1/chat` does, while
the voice is making its sound ([ADR
0023](decisions/0023-audio-out-the-voice-of-an-answer-is-streamed-from-the-gateway-to-the-device.md)):

| Event | Says |
| --- | --- |
| `format` | `sample_rate` (and `pcm_s16le`: 16-bit little-endian samples, one channel): first, and only when the voice has sound to give |
| `samples` | `data`: whole samples, base64, at most 64 KiB; about a tenth of a second at a time |
| `done` | the voice has said it all |
| `error` | `code`, `message`: it failed after it had begun |

Only `done` says a voice is whole: a stream that ends without it, or with an `error`, was cut off
(`GatewaySpeech.synthesize_stream` raises it, typed, and never ends as if it had finished). A voice
that cannot begin (no `tts` binding, a provider that is down after its retries) is an HTTP error with
its own status, as for a chat, and a voice with nothing to say is `done` alone. Piper makes one
piece of audio for each sentence, each on a thread of its own and only when the next is asked for;
the gateway puts the pieces together (a provider may end one in the middle of a sample) and cuts
them into chunks, so whatever a provider hands over, a client sees the same stream. Closing the
connection stops the voice: the sentence that was being made is finished and thrown away, and no
other is made. The profile of the `tts` role says `streaming: true`.

The engine for a binding's model is built the first time it is actually asked to transcribe or to
synthesize, not at startup: a deployment that never binds either role never loads one. Building it
downloads the model from Hugging Face if it is not already cached — a few hundred MB for a Whisper
size, tens of MB for a Piper voice — into `FACTOTUM_GATEWAY_SPEECH_CACHE_DIR` (default: each
library's own cache in the user's home directory); `FACTOTUM_GATEWAY_SPEECH_DEVICE` (`cpu` or
`cuda`) picks where both run. A path is never downloaded and a name is: give `model` the folder of a
Whisper model, or the `.onnx` file of a voice, where nothing may be fetched. On `cuda`, ctranslate2
needs cuBLAS 12 on the `PATH` (cuDNN 9 comes inside its wheel).

What is not speech is filtered out of a recording before the model listens (the voice activity
filter that faster-whisper carries inside its package, with no download): told to hear Italian,
Whisper writes «Sottotitoli e revisione a cura di QTSS» for a second of silence or of faint noise,
and the assistant would answer that as if somebody had asked it.

faster-whisper reads a recording with PyAV, and the gateway holds `av` below 19: faster-whisper
1.2.1 passes `av.open` an argument that PyAV 19 removed, so with it no recording could be read.
`tests/test_speech_decoding.py` is what fails first when a lock moves past that. See [ADR
0021](decisions/0021-voice-wake-word-on-the-device-speech-through-the-gateway.md) for why these
two, in the gateway's own process, rather than a hosted provider.

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
- **Ollama makes a grammar of the schema**, and two things about it were measured (version 0.34): a
  schema that says nothing (`{}`, "a value of any kind") is an *object* to it, so a model asked for a
  text there wrote `{"hello world": ""}` and for a number `{"value": 42}` (say `anyOf` the types
  instead); and a `maxLength` of exactly 2000 makes it fail with an HTTP 400, "failed to parse
  grammar" (1999 and 2001 are taken). `pattern` and `anyOf` are enforced. It cost a whole class of
  skills before it was found ([`skills.md`](skills.md#what-the-measurements-found)).

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

## Where your data goes, and which networks the gateway may reach

The gateway sends a request to the model the user bound, and does not judge where that model runs.
Whoever chooses a hosted provider has chosen to send their prompts and documents there; whoever wants
nothing to leave the machine runs Factotum on premise with a self-hosted model such as Ollama
(ADR 0010). There is no data classification on requests or collections, and a binding does not
declare where its model is. A request or a bindings file that still carries one is refused as an
unknown field, so nobody believes something enforces it.

What the gateway does restrict is the network it may reach, because that is a security matter and it
belongs to the deployment. A single-tenant (on-premise) deployment may connect to its own network,
where an Ollama or a vLLM usually is (`localhost`, the LAN, `host.docker.internal`). A multi-tenant
one reaches the public internet only, because the network is shared: it follows
`FACTOTUM_TENANT_MODE`. In both, cloud metadata addresses are never reachable, every resolved address
is checked and the connection goes to the address that was checked, and redirects are not followed.
A `ModelGateway` that is not told otherwise is built for the public internet only.

**Connections are kept between calls.** Every call gets an HTTP client of its own, with the timeout
of its binding and nothing (a cookie, say) that another call left behind, but the connections under
it are lent by one transport that lives as long as the gateway: a provider that was called a moment
ago is not connected to again, and a hosted one not negotiated TLS with again. There is a pool for
each host name (never shared by two names that happen to resolve to the same address, since each
connection was made, and its certificate checked, for one name); the 32 used last are kept, at most
16 idle connections each and for 30 seconds, and a pool somebody is still reading from is never
closed to make room. The service closes them all when it stops (`ModelGateway.aclose`).

## Secrets and logs

- Keys live in the environment and in the gateway's memory (`SecretStr`, never in a `repr`), and go
  in a header, never in a URL or a body.
- Provider error messages are redacted and cut to 500 characters, because providers sometimes quote
  the key they were given. Only the type of a transport error is reported, since its message may
  quote the address.
- One log line per call: role, tenant, provider, model, outcome, duration, token counts.
  **Never the conversation.** A test checks that neither the prompt nor the key reaches the log.
- A request that breaks the contract gets a 422 that says where and why, never what: the value could
  be the text of a conversation or a key.

## The HTTP API

For the other services, never for the outside world. Authentication is the hub's once a token secret
is configured (`FACTOTUM_HUB_TOKEN_SECRET`, see [`hub.md`](hub.md)); otherwise the development
authenticator (`x-factotum-dev-subject: user|tenant`), so **the gateway refuses to start outside
`FACTOTUM_ENVIRONMENT=development` or `test`** without one set.

| Route | Body → answer |
| --- | --- |
| `POST /v1/chat` | `ChatRequest` → `ChatResult`, or server-sent events with `stream: true` |
| `POST /v1/embeddings` | `EmbeddingRequest` → `EmbeddingResult` (`vectors`, `dimensions`, `space_id`, `usage`) |
| `POST /v1/transcribe` | `TranscribeRequest` (`audio`) → `TranscribeResult` (`text`, blank if nothing was understood) |
| `POST /v1/synthesize` | `SynthesizeRequest` (`text`) → `SynthesizeResult` (`audio`), or server-sent events with `stream: true` |
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
   `native_structured_output`, `chat`, `chat_stream`, `embed`, `transcribe`, `synthesize` and
   `synthesize_stream` (a provider that makes the clip whole yields it as one `SpeechPiece`). A
   provider that does not offer one of these raises `role_not_supported` (`base.role_not_supported`)
   rather than pretending; `whisper_local` and `piper_local` do this for everything but speech. Use
   the plumbing of `providers/base.py` (typed errors from statuses, redaction, the two stream
   readers, retry-after). Read the provider's own documentation and types: write the wire models in
   the adapter.
2. Register it in `default_adapters()` in `core.py`, and its default address if it has one.
3. Add its wire format to `tests/test_adapter_contract.py`, and its own file of tests for what only
   it does.

## Known limits

- The gateway does not count tokens: the provider says when a prompt does not fit
  (`context_too_long`), and callers budget with the profile.
- Nothing limits how many connections a busy gateway opens to one provider at once, or how many
  calls a tenant may have in flight: quotas come with the cloud gates.
- Asymmetric embedding models (a different prefix or task for a query and for a document) are not
  modelled yet; the knowledge service will decide how.
- **No hosted speech provider yet.** `stt` and `tts` are served locally (`whisper_local`,
  `piper_local`) only; every hosted adapter answers `role_not_supported` for both, even where the
  provider itself offers one (OpenAI's Whisper and TTS APIs, for instance). Adding one is the same
  three steps as any other provider, above.
- **Keys come only from the environment, by design, even from the hub**: a binding the hub holds
  still only ever names an environment variable (`secret_ref: env:...`), read where it always was.
  Letting an administrator paste a provider's key into the hub instead, encrypted at rest, was on
  the original plan for this slice ([ADR
  0019](decisions/0019-model-bindings-in-the-hub-still-only-a-reference-to-a-key.md)); it is not
  done, to avoid a store for secrets the platform does not otherwise need yet (no deployment here is
  one where a shared environment variable does not already work). It would fit naturally with
  wherever a skill's own secrets end up (a later slice), which needs the same answer to the same
  question.
