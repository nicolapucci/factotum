# The hub

The platform's own administration: tenants, users and devices, spaces, linked accounts, model
bindings and the secrets of skills (`docs/design.md#3-architecture-planned`). It is the backend of
the web app (slice 9), not a step in a conversation's turn: a speaker still talks to the edge, which
asks the conversation service, exactly as before. **It knows no document and no plaintext key.**

*Implemented* (slice 7a only): the service itself, a tenant and its administrator made once at the
first start, logging in, changing a password, adding a user to a tenant, and the tokens that prove
who a request is for to every other service. *Not yet*: a device's pairing (slice 7b, until then the
edge and the actions service still read a file), spaces, linked accounts and model bindings (7c),
skill secrets (7d), and any web app to use this from (slice 9: today it is an HTTP API only, tried
with `curl` below).

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

## The HTTP API

| | |
| --- | --- |
| `POST /v1/auth/login` | `{username, password}` → `{token, must_change_password, expires_at}`. No caller needed: it is how one is made |
| `GET /v1/me` | The user of the caller's own token |
| `POST /v1/auth/change-password` | `{current_password, new_password}` → `204` |
| `POST /v1/users` | Add a user to the caller's tenant (an administrator, not pending a password change): `{username, password, roles}` → `201` |
| `GET /v1/users` | The users of the caller's tenant (an administrator only) |
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

## Tests

Unit tests with a store in memory; the same contract tests run again against a real Postgres
(`pytest.mark.integration`), including that the administrator is made once and kept across a
restart. `factotum.common.tokens` and `factotum.common.hubauth` are tested on their own: a token
round-trips to the same `Principal`, a tampered payload or signature is refused the same way as a
token that is not one, and an expired one stops working at the second it says it would.

## Known limits

- **No web app yet** (slice 9): everything here is tried with `curl`, as the other services were
  before their place in the pipeline had a client of its own.
- **No device pairing yet** (slice 7b): `config/devices.dev.yaml` is still what the edge and the
  actions service read, and only in development.
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
- **Linked accounts, model bindings and skill secrets are not here** (7c, 7d): a deployment still
  configures the gateway and a skill's secrets from files, as before.
