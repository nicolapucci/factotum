# Architecture decisions

Short records of the decisions that shape Factotum and the reasons behind them. Each one states the
context, the decision and what follows from it. To change a decision, add a new record that supersedes
the old one instead of rewriting history.

| # | Decision |
| --- | --- |
| [0001](0001-monorepo-with-a-uv-workspace.md) | Monorepo with a uv workspace |
| [0002](0002-postgres-with-pgvector-for-all-state.md) | Postgres with pgvector for all state |
| [0003](0003-tenants-users-and-subjects.md) | Tenants, users and subjects |
| [0004](0004-permissions-never-depend-on-a-voice.md) | Permissions never depend on a voice |
| [0005](0005-system-and-external-devices.md) | System devices and external devices |
| [0006](0006-skills-and-sandboxed-execution.md) | Skills and sandboxed execution |
| [0007](0007-approval-is-out-of-band-and-fails-closed.md) | Approval is out of band and fails closed |
| [0008](0008-the-model-gateway-is-the-only-place-that-knows-models.md) | The model gateway is the only place that knows models |
| [0009](0009-on-premise-first-cloud-later.md) | On-premise first, cloud later |
| [0010](0010-where-data-goes-is-the-users-choice-of-deployment-and-providers.md) | Where the data goes is the user's choice of deployment and providers |
| [0011](0011-knowledge-pieces-to-search-and-sections-to-read.md) | Knowledge: pieces to search, sections to read, and a search that degrades |
| [0012](0012-conversation-no-history-in-a-turn-a-log-to-search-when-referred-to.md) | Conversation: no history in a turn, and a log to search when a message refers to it |
| [0013](0013-the-edge-typed-parts-filtered-by-what-a-device-can-render.md) | The edge: typed parts, filtered by what a device can render |
| [0014](0014-actions-the-model-proposes-the-code-decides.md) | Actions: the model proposes, the code decides |
| [0015](0015-the-runner-a-container-for-each-run-and-a-proxy-that-checks-a-grant.md) | The runner: a container for each run, and a proxy that checks a grant |
| [0016](0016-skills-that-are-instructions-a-model-writes-a-plan-the-code-decides.md) | Skills that are instructions: a model writes a plan, the code decides, the sandbox runs it |
| [0017](0017-hub-tokens-a-signed-principal-every-service-can-check.md) | The hub's tokens: a signed `Principal` every service can check |
| [0018](0018-device-pairing-a-long-lived-credential-checked-without-the-hub.md) | Device pairing: a long-lived credential, checked without the hub |
| [0019](0019-model-bindings-in-the-hub-still-only-a-reference-to-a-key.md) | Model bindings in the hub: still only a reference to a key |
| [0020](0020-skill-secrets-in-the-hub-plaintext-not-encrypted.md) | Skill secrets in the hub: plaintext, not encrypted |
| [0021](0021-voice-wake-word-on-the-device-speech-through-the-gateway.md) | Voice: the wake word stays on the device, speech goes through the gateway |
| [0022](0022-audio-in-is-streamed-to-the-edge-and-the-device-ends-the-utterance.md) | Audio in: a stream of chunks to the edge, and the device ends the utterance |
| [0023](0023-audio-out-the-voice-of-an-answer-is-streamed-from-the-gateway-to-the-device.md) | Audio out: the voice of an answer is streamed, from the gateway through the edge to the device |
