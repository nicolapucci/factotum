# 0020. Skill secrets in the hub: plaintext, not encrypted

Status: accepted, 2026-10-01. Depends on
[ADR 0019](0019-model-bindings-in-the-hub-still-only-a-reference-to-a-key.md).

## Context

A skill's secret (the token of a home's Home Assistant, a router's password) has no environment
variable to point at instead of itself, unlike a model binding: it is set by an administrator for
one tenant or one person, not configured once for a shared gateway container, so there is nothing
already on the machine running the actions service for it to reference. The hub has to hold the
value, recoverable, because the actions service needs it back in the clear to run a skill with it
([`actions.md`](../actions.md)). ADR 0019 left this question open on purpose, calling it "the same
question asked twice": whether to encrypt what the hub holds.

Unlike a device's credential or a model binding's reference, there is no hash to keep instead (a
secret must be read back, not only matched) and no reference to keep instead (there is nothing
else for it to point to). Something recoverable has to be stored; the question is only whether it
is encrypted where it sits.

## Decision

**Secrets are stored as plain text in Postgres**, in `hub.secrets`, with no encryption and no new
dependency. An operator was asked directly, choosing this over adding a symmetric-encryption
library and a key the hub would need to be given and keep safe: the same database already holds
password hashes and device credential hashes, under the same access control, and a deployment that
trusts it with those trusts it with this.

Setting the same name again, for the same owner, replaces it rather than being refused, the same
choice already made for a model binding ([ADR 0019](0019-model-bindings-in-the-hub-still-only-a-reference-to-a-key.md)):
rotating a secret is routine, not a conflict to catch.

## Consequences

- **A stolen database backup or disk is a stolen secret**, for whichever ones are set: encryption
  at rest, had it been built, would have protected against exactly this and nothing more (a
  compromised hub process already holds the decryption key, so it would not have protected against
  that). This is a real trade-off made deliberately, not an oversight.
- **No key for an operator to generate, back up, or lose.** Had a secret been encrypted, losing the
  key would have meant losing every secret at once, with no way back; plaintext has no such single
  point of failure, at the cost of the protection above.
- **The question is now closed for this slice**, not merely deferred again: the hub stores secrets
  the same way it stores everything else that is not a credential meant to be hashed, and nothing
  here is marked to revisit without a new, concrete reason to.
