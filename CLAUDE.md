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
# the same with no hardware: a recording in a file is streamed as a microphone would, and what is
# said comes back as a file (the client that tests the whole stack)
uv run factotum-terminal --profile speaker --wav-in question.wav --wav-out answer.wav
# the program of a real device on this machine, with a push to talk (docs/device.md): settings, check, run
uv run factotum-device init && uv run factotum-device doctor && uv run factotum-device
# the wake word of a device (no key): the models, fetched once, and your own voice against them
uv run factotum-device wake-word download && uv run factotum-device doctor --word
# the wake word with the real models, a synthetic voice and the real Whisper (docs/development.md):
FACTOTUM_LIVE_TESTS=1 uv run pytest -m live e2e/tests/test_e2e_wakeword.py -s
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
  and whom it acts for come from the registry, never from what it says. The sound of an answer is
  sent only to a device that said it takes it (`features`), is made on a task of its own so that the
  words never wait for it, and never follows the `turn_ended` of its turn (ADR 0023).
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
  with no URL nothing is asked, and when it cannot be reached the message is left to the documents
  and the turn says so in a warning (and in the sentence, if they have nothing relevant). Skills, the
  audit trail and everything that manages them are never served through a device.
- **The model alone does nothing** (ADR 0028). Whatever is asked, a question or a command, is
  satisfied by one or more skills, or answered from material that a model is *given* (the relevant
  passages of the documents, the earlier exchanges that a message refers to), or declined. A model
  never makes a fact of its own: it has no clock (the prompt does not tell it the date), no
  knowledge to add and nothing it can do, and with nothing to work on it is **not asked at all**
  (`TurnEngine._declined`: one sentence of `conversation/phrases.py` that the code chooses, in the
  language of the message read with no model by `factotum.common.language`). There are two kinds of
  "no" and a person is told which: **no skill fits** (`no_skill`, said before anything is tried) and
  **a skill that was found and failed**, which names the skill (*La skill weather ha fallito*) or
  says why in words of its own (an `error` where a script would print a `message`; a script that
  crashes says nothing, and its standard error never reaches a person). Relevance is decided in code
  before a model sees a passage, on the raw similarity of the vector search (`Passage.similarity`,
  never the fused `score`, which only says how high a passage came) with a floor that belongs to the
  embedding model (`min_similarity`; what only the words found is not relevant, except a code as
  written). The documents behave as a skill (a name, a phase, a failure with words) and are one in the
  end. Nothing may treat the time, or anything else, as a special case: the assistant behaves the same
  whether the core skills exist or not. A required value is asked for and an optional one that was not
  said is left to the skill or the device (the second slice of the ADR, not built yet). Several skills
  in one request are not built, and nothing may assume that there is exactly one.
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
- **What a device is made to say is a clip, and the platform makes it** (ADR 0026). `play_audio` (a
  `text` and the clip of it) is the platform's command: what a skill, a script or a plan may give is
  `SkillCommand`, the vocabulary without it (`SKILL_COMMAND_MODELS`: the checks of a plan and the
  reading of what a script prints use it), so a skill of a person can never hand a device sound that
  nobody made. A `speak` that any skill gives is made a voice by the *edge* (the gateway's `tts` of
  the tenant, a clip for each sentence, each sent as soon as it is made) for a device that declared
  `play_audio`, and is sent as words to one that did not; a `play_audio` for such a device is sent as
  the words it carries. **`ok` of a clip is *accepted*, not played**: a device answers at once and
  says it when nothing else is, in order, from a task of its own (the chain from a message to a
  speaker is a request and an answer, and one that stays open for as long as a sentence lasts is not
  worth having); it holds a bounded amount of what it has not said, and a person who speaks cuts what
  is being said. The commands of one run are given in order and the first that is not done stops the
  rest. Do not make a device wait for a clip to end, and do not let a model or a script give one.
- **A core skill is code of the platform, and what makes a version one is the hash of its text**
  (ADR 0026). `factotum.actions.core`: a `SKILL.md` that the code carries (matched, read by the
  planner, authorized, confirmed and audited as any skill is) and a function that runs it in the
  actions service, with a port for what it needs (`SpeechMaker`) and never the service. A stored
  version is the platform's when its name and the hash of its content are the platform's: a skill of a
  person that is called `announce` and says something else is run in the sandbox as theirs, and
  nothing is gained by writing the platform's text. They are installed and enabled for a tenant the
  first time it is asked for (a message, or its administrators looking at the catalog) and **one that
  was disabled stays disabled**; a new text is enabled where it ships. No model writes a plan for
  one and no sandbox runs one. A core skill reads what it is given as the input that it is (it is
  called with what a message said, and later with what other skills say), limits what it makes (a
  clip costs a model's time), says what failed as a kind (the code of the error, never what a service
  said) and is tested with two tenants. Skills calling each other is not built: it is a decision of
  its own.
- **A client is the core and the ports of its device** (ADR 0024). What every client does of the
  protocol (`hello`, the parts of an answer, commands and their `ack`, the sound of an answer, speaking
  over it, a stream sent as a person speaks, staying connected) is `factotum.client_core` and nowhere
  else: a client has the hardware of its device and none of the protocol, and the core has no
  `sounddevice`, no `numpy` and no terminal. A client declares what it was given and nothing more
  (`voice_stream` only with a sink and a speaker, `play_audio` only with a sink or a speaker of clips;
  the capabilities of its `Identity`). The sound that plays is cut *before* the edge is told that the
  person speaks or cancels, what is still in flight for an answer that was left is dropped in silence
  (it is nobody's bug), and what the edge gets wrong (a clip for a device that did not say it plays
  them, sound it did not ask for) is a `violation`, said loudly. `stay_connected` does not ask again of an edge that refused the
  device or replaced it with its twin. Do not copy the protocol into a client.
- **A device application is the core, the library of the sound hardware and its settings** (ADR 0025).
  Everything that is a sound card goes through `AudioBackend` (`factotum.client_audio`) and
  `sounddevice` is imported in one place, when it is first needed. A device declares only what it has
  hands for (`audio_in`, `audio_out`), and what it has no hands for (what is said is not the hands'
  work: the core plays a clip, and a text never reaches a device that takes clips) is *unsupported*
  and the skill is told, never `ok`. The volume is the speaker's own, in software, and the system's
  is left alone. The secrets of a device (its credential, and the key of Porcupine when that is the
  engine) are in the environment, which wins over the file, and in no line it says and no error. A
  push to talk needs a keyboard, so a service listens for a word. A supervisor restarts a device for
  what failed (exit code 1) and never for what the edge decided (3 to 6: asking again is the two
  connections of one device taking it from each other for ever). `doctor` connects *as the device*,
  and the edge keeps the last connection: `--no-edge` when one is running.
- **The wake word is an engine behind a port, and the models of the default are not ours** (ADR 0027).
  `WakeWordEngine` is what `WakeWord` asks, a frame at a time, and `wake_word_engine` is the one place
  that says which engine takes what (the device and the terminal both ask it, and a client never
  chooses): openWakeWord, which needs no account and no key, is the default; Porcupine needs a key
  that Picovoice gives to companies (it has no free plan) and is an extra (`--extra porcupine`). The
  models of openWakeWord are their author's (CC BY-NC-SA 4.0, for use that is not commercial): they
  are never in the repository or an image, and are fetched only by `factotum-device wake-word
  download` (one address in the code, a size and a SHA-256 pinned in `WAKE_WORDS`, a body that is not
  the file is never kept). A device or a test that has none says what to run and does not fetch.
  Do not fetch them from anywhere else, do not build an engine but through the factory, and do not
  leave an engine remembering what a person said: it is made to forget (`reset`) after each utterance.
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
  fit (the speech roles were broken for every binding written the documented way until one did). Each
  of its tests runs with a recording sent whole and with it streamed (ADR 0022), and the voice of
  the answer a clip or a stream (ADR 0023).
- **A command that a person can say is a case of the corpus** (`factotum.actions.plan_cases`), and the
  series of `e2e` says every one of them to the three devices of the example home and judges what the
  whole assistant did (`factotum.e2e.series`: the decision, what ran and with what, what a device was
  told and did, the audit trail, no rule of the protocol broken). A new skill, or a new way for an
  action to come out, is added there, with what the code must make of it, and not only tested on its
  own.
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
- What the stand-in of a model writes in a test (`HouseholdModel.proposal`, `.writing`) has to fit
  what the gateway makes a model answer with, or it is no answer at all: a `GET` written without
  `headers` is `invalid_structured_output` three times over, and the skill is a skill that failed.
  `factotum.e2e.series` checks its plans against the schema in its own tests.
- Whisper hears a loanword that a voice says the Italian way as something else, whatever its size
  (measured with Piper's `it_IT-paola-medium` and the 25 Italian commands of the series: "apri il
  garage" came back as "A pigara!" and "riavvia il router" as "Ja, via Lutte!"; `small` heard "a 70" as
  "a 60", and `large-v3` heard every number). The names of what is in the home are what a command is
  about, and the `stt` binding can list them for the model (`options.vocabulary`, off unless it
  does): on the 26 commands `medium` heard 21 as said instead of 17 and `large-v3` 23 instead of 21,
  and the loanword commands stay wrong with `large-v3`. It is a prompt, so it is a list of words and
  never a sentence, and what a model is given it may say when nobody said it (`large-v3` heard a
  lone "sì" as "Sì, va bene."): try it with the series (`FACTOTUM_LIVE_VOCABULARY`) on the models and
  the voice it is for. Do not take a command that was heard well once for one that is.
- Tests import `--import-mode=importlib`: one test module cannot import another, so what tests share
  lives in a `testing.py` in the package (`factotum.edge.testing`, `factotum.terminal.testing`,
  `factotum.client_core.testing`: the fakes of every port of a client, and `ClientRig`;
  `factotum.client_audio.testing`: a sound card and a key that a test plays; `factotum.device.testing`:
  settings and an edge that a test plays).
- A fake that is looser than the real thing hides what the real thing does: the key of a test
  (`KeyPresses.pending`) never looked at the presses that were waiting, which `EnterKey` does, so a
  press that came after what was said was over began another thing to say and no test noticed. A test
  of the pair feeds the real key a line at a time (`Typing`, in `test_client_audio_inputs.py`), and a
  fake that is asked something remembers what it was asked (`FakeBackend.checked`), or what was
  asked of it at the wrong rate looks right.
- `logging.basicConfig(level=...)` does nothing once the root logger has a handler (pytest's, a
  library's): the level is set on the logger. Under a service standard input is the end of a file and
  standard output a pipe (not written until it is full: a line is flushed as it is written), so a
  push to talk ends at once and says so.
- Windows lists the same hardware once for each host API (45 entries for a handful of devices) and
  `sounddevice` refuses a part of a name that fits more than one: a device is chosen by its number.
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
  "apri il garage", Polish for "no, grazie"): an `stt` binding hears Italian unless its
  `options.languages` lists others (`null` there brings the guessing back). And faster-whisper
  cannot read a recording with PyAV 19 (it passes `av.open` an argument that was removed), so `av` is
  held below 19. Told to hear Italian, Whisper writes the credits of a subtitle for a second of
  silence ("Sottotitoli e revisione a cura di QTSS"), so what is not speech is filtered out first. A
  stand-in engine showed none of this: it took a real model, so a change to the speech path has to be
  tried with one (`e2e/tests/test_e2e_speech.py`), not only with the stand-ins.
- A client over a connection that is gone must not say anything into it, and a test cannot always
  tell: an in-memory connection that was dropped still takes a frame where a real one raises, so a
  loop that says the next thing as the connection ends waits for ever for its answer (a run over
  several files did). Do not patch `asyncio.sleep` in a test while a client is connected: its
  heartbeat sleeps with it.
- The services of `e2e` are joined by `httpx.ASGITransport`, which hands a response over when it is
  whole: nothing there shows an answer arriving a sentence at a time (the real sockets of the edge's
  own tests do, and so does `e2e/tests/test_e2e_voice_wire.py`, which serves the gateway on one).
  What can be shown there is that an answer that is being written is cut, and its model let go of
  (`HouseholdModel.hold_after`, `abandoned`), and that a voice that is made slowly neither holds
  back the words nor survives a person speaking over it (`StandInPiper.hold_after`: the thread that
  waits is waited for when the test ends, so a test that holds it lets it go in a `finally`).
- Piper loads its voice when it is first asked to say something, 4.9 s on a processor: whatever
  waits for the gateway waits more than httpx's default of five seconds for every call. The edge's
  client did, and the first answer after a start would have been words with no sound
  (`FACTOTUM_EDGE_GATEWAY_TIMEOUT_S`, default 60).
- The keys of `extra=` in a log call must not be attributes of a `LogRecord` (`name`, `message`,
  `args`...): the standard library raises `KeyError`, and only when the level is enabled, which is
  how a session died the first time the log was at INFO.
- A script that rewrites a source file for a mutation test has to write bytes: `Path.write_text` on
  Windows turns `\n` into `\r\n`, and git then warns of the line endings of a file that was never
  meant to change. And a subprocess that is killed must be killed with its tree (`taskkill /F /T`):
  `uv run pytest` is `uv`, which starts `pytest`, which starts `python`, and the last one holds the
  pipe.
- openWakeWord scores zero for the first five frames after an engine is made or told to forget (its
  buffers hold noise until then): a probe or a test that listens for fewer than six frames sees
  nothing, and the word is said to nothing if it comes first. What a synthetic voice scores is not
  what a person's does (Piper's Italian voice says `hey_jarvis` at 0.21 and `alexa` at 1.00): the
  threshold is chosen with `factotum-device doctor --word` and a real voice, and never from a test.
- A default that changes is a test that waits: a device given `mode: wake_word` and a key was, with the
  new default engine, a device that needs no key and waits for models that were not there, and the test
  did not fail, it waited for an edge that never answered. A test of a device says which engine.
- The home of whoever runs the tests is not theirs to have: `libs/client_audio`, `clients/device` and
  `clients/terminal` run every test with an empty `HOME` and `USERPROFILE` (their `conftest.py`:
  `Path.home()` and `~` both read them), because a machine that had fetched the models of the wake
  word passed a test of the device that a clean one fails (found by a mutation: the device was not
  given its engine, and the real one on the maintainer's machine heard no word either). A test that
  wants a folder says it (`models_dir`).
- The `e2e` conftest has an autouse fixture, `_how_the_house_is_run`, that runs a whole house three
  ways for every test of the folder: a module that is not about the house (the wake word is a device's)
  replaces it by defining a fixture of that name, or each of its tests is run three times.
- A test of a turn that expects a model's answer gives the turn something to answer from
  (`harness.documents.passages = [passage()]`, or an exchange that the message refers to): with
  neither, and no skill, the model is not asked and the answer is the sentence of the code (ADR 0028).
  A setup turn that must be answered by the fake chat needs a passage too, then the passages are
  cleared for the turn that is about the past. The house of `e2e` uses the floor of its stand-in model
  (`STAND_IN_FLOOR`, 0.25: it measures the words two texts share), not the 0.5 of `bge-m3`, and a
  question of a test must share enough words with the document to be relevant to it.
- The score of a passage is not its relevance: it is a fusion of ranks scaled by the best a rank can
  score (a question about nothing has a first passage at 1.0). Ask for `min_similarity` and read
  `similarity`. The floor is measured with the real model and the numbers are in `docs/knowledge.md`
  and `docs/conversation.md`: another embedding model needs its own, and a stand-in needs its own.
- A model that is told to say only what the sources say still answers *restart the router* with the
  way to erase its settings if the manual is near (measured with llama3.1 8B): the prompt says that a
  message which asks for something to be done is not a question for the sources, and the live tests
  (`services/conversation/tests/live`) keep that line honest. The second line of defence is
  probabilistic; the first (the floor, and not asking a model with nothing to work on) is not.
