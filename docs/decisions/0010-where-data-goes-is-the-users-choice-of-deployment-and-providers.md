# 0010. Where the data goes is the user's choice of deployment and providers

Status: accepted, 2026-09-20. Supersedes the data-policy paragraph of
[0008](0008-the-model-gateway-is-the-only-place-that-knows-models.md) and the `local` or `remote`
attribute it gave to a binding.

## Context

ADR 0008 gave every collection a data policy (`local_only` or `external_allowed`), gave every model
binding a locality (`local` or `remote`), and had both the conversation service and the model gateway
enforce that a `local_only` collection never reaches a `remote` model. The gateway was built that way
in slice 1.

The maintainer's judgement, on review, is that the policy is redundant. Whoever runs Factotum in the
cloud with hosted providers has already accepted that their documents are handled there. Whoever
wants nothing to leave their machine runs it on premise with a self-hosted model such as Ollama. The
choice is made once, by picking the deployment and the providers. The application has no need to have
it declared again, to record it or to police it.

## Decision

- There is no data classification and no locality anywhere: not on collections, not on requests, not
  on bindings, not in profiles. A request or a bindings file that still carries one is refused as an
  unknown field rather than ignored, so that nobody believes something enforces it.
- The gateway does not judge where a model runs. It sends a request to the model the user bound.
- One thing stays, and it is a security property of the deployment rather than a data policy: which
  networks the gateway may reach when it calls a provider (the URL guard, see the design). A
  single-tenant (on-premise) deployment may reach its own network, which is where a local model
  usually is. A multi-tenant deployment reaches the public internet only, because the network is
  shared. Cloud metadata and other forbidden addresses are refused in both. The rule follows
  `FACTOTUM_TENANT_MODE`; nobody declares it per binding.

## Consequences

- Simpler contracts (`ChatRequest`, `EmbeddingRequest`, `ModelBinding`, `Profile`), no collection
  policy in the knowledge design, and nothing for the conversation service to check.
- Factotum makes no promise about where a mixed setup sends data (a hosted chat model with local
  embeddings, or the reverse): the user knows what they bound. The documentation says plainly what
  each choice means.
- The old cloud gate "no `local` bindings" (ADR 0009) is already how a multi-tenant deployment
  behaves.
- If an organization ever needs a guarantee, its place is administration (which providers a tenant
  may bind, in the hub), not a classification carried by every request. That would be a new decision.
