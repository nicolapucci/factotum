# 0019. Model bindings in the hub: still only a reference to a key

Status: accepted, 2026-10-01. Depends on
[ADR 0017](0017-hub-tokens-a-signed-principal-every-service-can-check.md).

## Context

The original design for the model gateway's secrets
(`docs/design.md#7-models`, written before the hub existed) called for keys "plaintext only in the
gateway's memory, encrypted at rest, write-only APIs" once something kept them — in other words, an
administrator would eventually paste a hosted provider's key into the hub, which would hold it
encrypted and hand it to the gateway decrypted, never echoing it back. Until then, "for now a binding
names an environment variable, and the gateway reads the key from it" was the stand-in.

Moving model bindings into the hub (so an administrator configures `chat.main` once, from the hub's
API, instead of hand-editing a file on the gateway's host) raised the question this slice actually
had to answer: build that encrypted store now, or keep the environment-variable reference as the only
way a binding ever points at a key, with the hub managing just the rest of a binding (role, provider,
model, address, owner).

## Decision

**A binding in the hub still only ever names an environment variable, never a key.** `secret_ref` on
`CreateModelBindingRequest` is the same `env:VARIABLE_NAME` shape `ModelBinding` already had; the hub
stores it as a plain string, same as it stores everything else about a binding, and the gateway
resolves it exactly as before (`EnvSecretResolver`, unchanged). No encryption, no key held anywhere
but the gateway's own environment and, briefly, its memory.

**No library for encryption was added.** Signing a token by hand (ADR 0017) is simple enough to get
right without one; encrypting a secret that must later be decrypted correctly, with a nonce that is
never reused, is a different and larger risk to take on for a feature nothing here yet needs — every
deployment today is on-premise, with its own environment to put a key in (ADR 0009). The question is
revisited if a deployment ever needs the hub to hold a key nobody can put in an environment variable
for it (a cloud, multi-tenant one, perhaps): the natural place to answer it is wherever a skill's own
secrets end up (slice 7d), since it is the same question asked twice.

## Consequences

- **The hub can configure a binding, but never supply a key that was not already on the gateway's
  machine.** A hosted provider still needs its key set in the gateway's own environment first,
  exactly as without the hub; the hub only spares an administrator from editing a YAML file on that
  machine for everything else about the binding.
- **"Encrypted secrets at rest," as `docs/design.md` first described the gateway's secrets, is not
  built.** The sentence is corrected rather than fulfilled: there is nothing encrypted because there
  is nothing held, which is a stronger guarantee for an on-premise deployment than encryption would
  have been, not a weaker one.
- **Binding the same role again, for the same owner, replaces it rather than being refused** (unlike
  a device's name or a skill's binding to one): choosing a different model for `chat.main` is
  something an administrator is expected to do more than once, not a mistake to catch.
