# 0018. Device pairing: a long-lived credential, checked without the hub

Status: accepted, 2026-10-01. Depends on
[ADR 0017](0017-hub-tokens-a-signed-principal-every-service-can-check.md).

## Context

[ADR 0017](0017-hub-tokens-a-signed-principal-every-service-can-check.md) gave the hub a signed,
self-contained token for a session and for one service proving a caller to the next, both short-lived
by design, and explicitly left open what a system device (a speaker, possibly off for a week) should
hold instead: it cannot refresh a token every sixty seconds, or every twelve hours, by asking a hub it
may not be able to reach yet. Until now the edge and the actions service read devices, and the skills
bound to them, from a YAML file maintained by hand on each deployment
([ADR 0005](0005-system-and-external-devices.md)); the hub needed its own way to register a device,
hand it something to hold, and let both services learn the result without asking it on every
connection or every message — the same no-hub-in-the-hot-path principle ADR 0017 already set for
tokens.

## Decision

**Pairing mints an opaque, long-lived credential, not a signed token.** `POST /v1/devices` on a
system device generates one (`secrets.token_urlsafe(32)`,
`factotum.common.credentials.new_credential`), returns it once (`PairedDevice.token`), and keeps only
its hash. It carries no claims and no expiry — unlike a login or a delegation token, nothing about it
needs to be read, only matched — so a device that is off for a week presents the same credential when
it reconnects, exactly as before pairing existed.

**The hash is a plain, fast one, not the slow hash used for human passwords**
(`factotum.common.passwords`, scrypt). A password is low-entropy and guessable, so hashing it slowly
is the point; a 32-byte random credential is not guessable to begin with, and a slow hash would only
cost every device, on every connection, for no defense it actually adds
(`factotum.common.credentials`, plain `hashlib.sha256`). Checking one still takes the same care a
password check does — `hmac.compare_digest`, and a dummy candidate hashed into every comparison so a
device cannot be timed into existing or not (`FileDeviceRegistry` already did this for the YAML file;
`HubDeviceRegistry` does the same against what the hub returned).

**The edge and the actions service each fetch their tenant's devices once, in their own lifespan, and
keep them in memory** (`HubDeviceRegistry`, `HubDeviceDirectory`), mirroring the file-based registry
and directory they already had rather than introducing a new shape: a device connecting, or a message
naming one, is checked against what was fetched, never against the hub itself. The actions service
also fetches the bindings of the tenant's skills to its external devices the same way. Neither
refetches on its own; nothing here adds a way to tell a running service that the hub's list changed
(the file-based registry has no such way either — a changed file needs a restart too, same as a
changed hub registration would today).

**A binding names a skill, not a skill's id.** The devices contract already had a `Binding`, keyed by
`skill_id: SkillId`, from before this slice; it turned out to be unused anywhere in the codebase — the
real interface (`DeviceDirectory.bound_devices(principal, skill_name)`, and the file format's own
`BindingSpec`) has always bound by the skill's name, because a binding can be made before, or without,
that skill being installed. The hub's `CreateBindingRequest`/`BindingView` match the real interface;
the old, unused `Binding`/`BindingId` contract is left as it was, not used by this slice and not
removed by it either.

## Consequences

- **A device's credential cannot be rotated or revoked.** Losing one, or suspecting it is no longer
  only the device's, means pairing again under a new name; there is no list of paired devices to
  invalidate one from, nor a reason yet to add one before there is a web app to do it from.
- **The file-backed registry and directory must never be constructed when the hub is configured, even
  to be discarded at once.** Both refuse to exist outside development and test
  (`factotum.contracts.errors.AuthError`), by design, so that forgetting to configure anything in
  production fails loudly; because of that guard, building one and then replacing it with the hub's
  is not equivalent to never building it — the first form made a correctly hub-configured production
  deployment refuse to start regardless. Both services now choose one or the other before building
  either.
- **Reading a device or a binding needs only an authenticated caller of the tenant, not an
  administrator.** The edge and the actions service act for the tenant, not for a person, to fetch
  this; only pairing a device or creating a binding is an administrator's act.
