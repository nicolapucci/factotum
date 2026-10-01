# The hub

The platform's own administration: tenants, users and devices, spaces, linked accounts, model
bindings and the secrets of skills (`docs/design.md#3-architecture-planned`). It is the backend of
the web app (slice 9), not a step in a conversation's turn: a speaker still talks to the edge, which
asks the conversation service, exactly as before. **It knows no document.**

*Implemented* (slices 7a-7d): the service itself, a tenant and its administrator made once at the
first start, logging in, changing a password, adding a user to a tenant, the tokens that prove who a
request is for to every other service, pairing a device and binding a skill to one, binding a role
to a model, and now the secrets a skill is given. *Not yet*: spaces, linked accounts, and any web
app to use this from (slice 9: today it is an HTTP API only, tried with `curl` below).

## Tokens, and how every service learns to check them

Until now, a service that needed to know who a request was for read it from a header nobody signed
(`factotum.common.devauth`, gated to development and test). The hub replaces that with a real,
**self-contained** token: a `Principal` (tenant, device, who it acts for, roles), signed, with an
expiry, in the shape of the grant that already protects a skill's sandbox
([ADR 0015](decisions/0015-the-runner-a-container-for-each-run-and-a-proxy-that-checks-a-grant.md)).
The reasons for this shape, and what it does not protect against, are in
[ADR 0017](decisions/0017-hub-tokens-a-signed-principal-every-service-can-check.md).

A service checks a token **without asking the hub**: every service that authenticates a caller
(`model-gateway`, `knowledge`, `conversation`, `edge`, `actions`, `runner`, and the hub itself) is
given the same secret (`FACTOTUM_HUB_TOKEN_SECRET`). Set, it uses the real `HubAuthenticator` and
`HubPrincipalDelegation` (`factotum.common.hubauth`); unset, it falls back to the development ones,
which refuse to exist outside development and test — so a production deployment without the secret
still does not start, exactly as before. The hub itself has no such fallback: it is the one that
mints, so it needs the secret in every environment.

## Bootstrap

In `single_tenant` mode (the default: one on-premise deployment), the hub makes its one tenant and
administrator the first time it starts, from `FACTOTUM_HUB_ADMIN_USERNAME` and
`..._ADMIN_PASSWORD` (`admin` / `admin` if neither is set). **Whichever password this is, the account
is created with it due to be changed at the first login** (`must_change_password`): every route but
`/v1/auth/change-password`, `/v1/me` and the health checks is refused until it has been
(`password_change_required`). A later start finds the tenant already there and does nothing: the
bootstrap credentials only matter once.

## Devices and bindings

A system device (Factotum hardware: a speaker with a microphone) is paired once: an administrator
calls `POST /v1/devices` with `kind: system`, a name, a `hardware_id` and its capabilities. The hub
mints a credential, hands it back once in the response (`PairedDevice.token`) and keeps only its
hash (`factotum.common.credentials`, a plain fast hash — the credential is already high entropy, not
a password that needs a slow one). An external device (a lamp, a garage door — anything Factotum
does not run on) is registered the same way with `kind: external`; it has no connection of its own
and so no credential. A binding ties a skill, by name (`CreateBindingRequest.skill`, not an id: a
binding can be made before the skill is installed), to a device, with that device's own parameters
(an entity id, for instance).

The edge and the actions service each fetch their tenant's devices once, in their own lifespan
(`GET /v1/devices`; the actions service also fetches `GET /v1/bindings`), and keep them in memory —
a connection or a message is never made to wait on the hub, the same choice as the tokens above.
Reading either list only needs an authenticated caller of the tenant, not an administrator: it is
how the edge and the actions service, which act for the tenant and not for a person, read it. The
reasons for this shape are in
[ADR 0018](decisions/0018-device-pairing-a-long-lived-credential-checked-without-the-hub.md).

## Models

`POST /v1/models` binds a role (`chat.main`, `embedding`...) to a model at a provider, for the
tenant or for the caller's own use — the same shape `config/bindings.example.yaml` already has, now
kept by the hub instead of a file. Binding a role that the same owner already bound **replaces it**:
unlike a device or a binding of a skill, choosing a different model for a role is routine, not a
conflict to refuse. **The hub never holds a provider's key**: a binding only ever names an
environment variable (`secret_ref: env:OPENAI_API_KEY`), exactly as a file-based one does, resolved
where the gateway already resolves it
([ADR 0019](decisions/0019-model-bindings-in-the-hub-still-only-a-reference-to-a-key.md) says why).
The gateway fetches its tenant's bindings once, in its own lifespan (`GET /v1/models`), the same way
the edge and the actions service fetch devices.

## Secrets

`POST /v1/secrets` sets the value of a secret a skill may be granted (a Home Assistant token, a
router's password), for the tenant or for the caller's own use, by name — the same shape
`config/secrets.example.yaml` already has, now kept by the hub instead of a file. A name must be one
a skill's manifest could actually declare (upper-snake-case, never one the runner itself sets); a
secret of the same name for the same owner is **replaced**, the same choice already made for a model
binding. **Unlike a device's credential or a model binding's reference, the hub genuinely holds the
value**, in plain text — there is nothing else for it to point to, and the actions service needs it
back in the clear to run a skill with it. [ADR
0020](decisions/0020-skill-secrets-in-the-hub-plaintext-not-encrypted.md) records why this is not
encrypted. The actions service fetches its tenant's secrets once, in its own lifespan
(`GET /v1/secrets`), the same way it fetches devices and bindings, and resolves a skill's own from
what it holds.

## The HTTP API

| | |
| --- | --- |
| `POST /v1/auth/login` | `{username, password}` → `{token, must_change_password, expires_at}`. No caller needed: it is how one is made |
| `GET /v1/me` | The user of the caller's own token |
| `POST /v1/auth/change-password` | `{current_password, new_password}` → `204` |
| `POST /v1/users` | Add a user to the caller's tenant (an administrator, not pending a password change): `{username, password, roles}` → `201` |
| `GET /v1/users` | The users of the caller's tenant (an administrator only) |
| `POST /v1/devices` | Pair a system device or register an external one (an administrator): `{kind, name, ...}` → `201`, a `PairedDevice` (system) or the bare record (external) |
| `GET /v1/devices` | The tenant's devices, system and external (any authenticated caller) |
| `POST /v1/bindings` | Bind a skill, by name, to a device (an administrator): `{skill, device_id, params, enabled}` → `201` |
| `GET /v1/bindings` | The tenant's bindings (any authenticated caller) |
| `POST /v1/models` | Bind a role to a model, for the tenant or the caller's own use (an administrator): `{role, provider, model, ...}` → `200`, replacing any binding the same owner already had for the role |
| `GET /v1/models` | The tenant's model bindings (any authenticated caller) |
| `POST /v1/secrets` | Set the value of a secret, for the tenant or the caller's own use (an administrator): `{name, value, owner}` → `200`, replacing any value the same owner already had for the name |
| `GET /v1/secrets` | The tenant's secrets, with their values (any authenticated caller) |
| `GET /healthz`, `GET /readyz` | Liveness; readiness (the database answers) |

Errors have the shape of the other services, `{"error": {"code", "message", "details"}}`. Logging in
with the wrong username or the wrong password is the same `auth_error` either way: which part was
wrong is not said. Interactive documentation is at `/docs` outside production.

## Configuration

Read from the environment (`FACTOTUM_*`) and, in development, from `.env`; every variable is in
[`.env.example`](../.env.example) and a test says so.

| Variable | Default | |
| --- | --- | --- |
| `FACTOTUM_HUB_HOST`, `..._PORT` | `127.0.0.1`, `8087` | loopback unless told otherwise |
| `FACTOTUM_HUB_TOKEN_SECRET` | none | shared with every service that authenticates a caller; the hub does not start without it, in any environment |
| `FACTOTUM_HUB_ADMIN_USERNAME`, `..._ADMIN_PASSWORD` | `admin`, `admin` | the administrator made at the first start (single-tenant mode only) |
| `FACTOTUM_HUB_SESSION_HOURS` | `12` | how long a token from logging in is good for |

The database is the one of `DatabaseSettings` (`FACTOTUM_POSTGRES_*`), in its own schema (`hub`).

## Trying it

```bash
FACTOTUM_HUB_TOKEN_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))") \
FACTOTUM_ENVIRONMENT=development FACTOTUM_POSTGRES_PASSWORD=dev \
    docker compose --profile services up -d --build hub postgres --wait

TOKEN=$(curl -s localhost:8087/v1/auth/login -d '{"username":"admin","password":"admin"}' \
    | python -c "import json,sys; print(json.load(sys.stdin)['token'])")

curl -s localhost:8087/v1/users -H "Authorization: Bearer $TOKEN"
# {"error":{"code":"password_change_required", ...}} — change it first:
curl -s localhost:8087/v1/auth/change-password -H "Authorization: Bearer $TOKEN" \
    -d '{"current_password":"admin","new_password":"something longer than that"}'
```

Logging in again after changing the password gives `must_change_password: false`, and a fresh token
reaches `/v1/users`. The same `FACTOTUM_HUB_TOKEN_SECRET` given to the other services lets a token
from here authenticate a call to any of them.

```bash
TOKEN=$(curl -s localhost:8087/v1/auth/login \
    -d '{"username":"admin","password":"something longer than that"}' \
    | python -c "import json,sys; print(json.load(sys.stdin)['token'])")

PAIRED=$(curl -s localhost:8087/v1/devices -H "Authorization: Bearer $TOKEN" -d '{
    "kind": "system", "name": "Kitchen speaker", "hardware_id": "kitchen-01",
    "capabilities": ["audio_in", "audio_out"]}')
echo "$PAIRED"  # {"device": {...}, "token": "..."} — the token is shown here and never again

DEVICE_ID=$(echo "$PAIRED" | python -c "import json,sys; print(json.load(sys.stdin)['device']['id'])")
curl -s localhost:8087/v1/bindings -H "Authorization: Bearer $TOKEN" \
    -d "{\"skill\": \"lamp\", \"device_id\": \"$DEVICE_ID\", \"params\": {}}"
```

Give `FACTOTUM_EDGE_HUB_URL` and `FACTOTUM_ACTIONS_HUB_URL` to the edge and the actions service (both
default to `http://hub:8087` in compose) and the hub's devices and bindings replace the file, the
moment `FACTOTUM_HUB_TOKEN_SECRET` is also set.

```bash
curl -s localhost:8087/v1/models -H "Authorization: Bearer $TOKEN" -d '{
    "role": "chat.main", "provider": "ollama", "model": "llama3.1:8b",
    "base_url": "http://host.docker.internal:11434"}'
```

`FACTOTUM_GATEWAY_HUB_URL` (also `http://hub:8087` in compose) does the same for the model gateway's
bindings.

```bash
curl -s localhost:8087/v1/secrets -H "Authorization: Bearer $TOKEN" \
    -d '{"name": "HA_TOKEN", "value": "the-token-of-the-home"}'
```

The actions service reads this the moment it reads devices and bindings from the hub: nothing more
to set than `FACTOTUM_ACTIONS_HUB_URL`, already given above.

## Tests

Unit tests with a store in memory; the same contract tests run again against a real Postgres
(`pytest.mark.integration`), including that the administrator is made once and kept across a
restart, that a device's `hardware_id` and a binding's device can only be each tenant's own, and
that binding the same role again, or setting the same secret name again, for the same owner,
replaces it instead of being refused. `factotum.common.tokens` and `factotum.common.hubauth` are
tested on their own: a token round-trips to the same `Principal`, a tampered payload or signature is
refused the same way as a token that is not one, and an expired one stops working at the second it
says it would. `HubDeviceRegistry` (edge), `HubDeviceDirectory` (actions), `HubBindingRepository`
(the gateway) and `HubSecretResolver` (actions) are each tested against a fake hub of their own, for
what they need: a registry proves a device by its hashed credential; a directory also resolves
bindings and the `enabled` and ownership filters the file-based ones already apply; a binding
repository resolves a model binding by tenant, owner and role; a secret resolver resolves a secret's
value the same way `FileSecrets` already does, including that a skill of one owner never sees
another's. `HubDelegation` (`factotum.model_client.gateway`) is tested on its own too: the token it
mints for an owner decodes to a `Principal` of the right tenant, acting as that owner, and expires
the same way.

## Known limits

- **No web app yet** (slice 9): everything here is tried with `curl`, as the other services were
  before their place in the pipeline had a client of its own.
- **One tenant.** Nothing here makes a second one: `multi_tenant` mode and any sign-up flow are not
  slice 7a.
- **A shared secret, not a key pair.** Any service holding `FACTOTUM_HUB_TOKEN_SECRET` can mint a
  token for any principal, not only verify one — the same trust every service already placed in each
  other through the development headers, now closed to whoever is outside the deployment instead of
  to nobody. Only the hub minting, and everyone else only verifying, needs asymmetric signatures
  instead ([ADR 0017](decisions/0017-hub-tokens-a-signed-principal-every-service-can-check.md)).
- **No sessions to see or revoke.** A token is good until it expires and nothing more: logging out,
  seeing who is logged in, or revoking one before it expires are not done (there is nowhere to do it
  from yet, either).
- **Linked accounts are not here.** A skill that declares one still fails and says so; nothing
  beyond the manifest's own declaration shape (`AccountRequirement`) exists for it yet, and it needs
  its own design — an account is an OAuth-shaped problem (a flow, a token to refresh, a callback),
  unlike a device's pairing or a secret's value, neither of which this slice's patterns answer.
- **A device's credential cannot be rotated or revoked.** Pairing mints it once; a lost or
  compromised one means registering the device again, under a new name.
- **A model binding never holds a provider's key**, not even encrypted: it only ever names an
  environment variable, as a file-based one already did
  ([ADR 0019](decisions/0019-model-bindings-in-the-hub-still-only-a-reference-to-a-key.md)). An
  administrator still sets the key itself on the machine that runs the gateway.
- **A skill's secret is kept in plain text**, unlike a password or a device's credential (both
  hashed) and unlike a model binding (never holding a value at all): there is nothing else for it to
  be, since the actions service needs it back in the clear
  ([ADR 0020](decisions/0020-skill-secrets-in-the-hub-plaintext-not-encrypted.md)). A stolen database
  backup is a stolen secret; the same access control the rest of the hub's data already relies on is
  what protects it.
- **Fixed to one tenant, for proving an owner to the model gateway and the knowledge service.**
  `HubDelegation` (`factotum.model_client.gateway`), unlike `HubPrincipalDelegation`, is built for
  `SINGLE_TENANT_ID` rather than threading a caller's own tenant through: `Owner` alone does not
  carry one. The same limit `HubDeviceRegistry` and `HubDeviceDirectory` already had for devices.
