# 0017. The hub's tokens: a signed `Principal` every service can check

Status: accepted, 2026-10-01. Depends on the signing shape of
[ADR 0015](0015-the-runner-a-container-for-each-run-and-a-proxy-that-checks-a-grant.md).

## Context

Every service decides what a caller may do from a `Principal` (tenant, device, who it acts for,
roles: `factotum.contracts.identity`). Until the hub existed, the only way to produce one outside a
test was a header nobody signed, which refuses to exist anywhere but development and test
(`factotum.common.devauth`). The hub has to issue something that works in production, and not only
the hub: every service that authenticates a caller needs to turn it back into a `Principal`, and a
service that calls another on a caller's behalf (the edge calling the conversation service, which
calls the actions service, which has the edge give a device a command) needs to prove the same one
downstream.

Two shapes were considered. An **opaque token** only the hub can resolve means every request that
carries a `Principal` costs a call to the hub first — a new synchronous dependency on every turn, and
a single point of failure the platform does not otherwise have (the conversation service already
degrades when the knowledge service is slow or down; nothing should need the hub to answer a device
that is only asking the time). A **self-contained, signed token** costs nothing to check once it is
minted, at the price of needing a secret everywhere a check happens.

## Decision

**A signed, self-contained token, in the shape of the egress grant**
([ADR 0015](0015-the-runner-a-container-for-each-run-and-a-proxy-that-checks-a-grant.md)): a version,
a base64url payload (the `Principal`, an issue time, an expiry), and an HMAC-SHA256 signature, joined
by dots (`factotum.common.tokens`). The hub mints one at login (and will at a device's pairing, slice
7b); a service that must prove the same principal to the next one in the chain mints a fresh one of
its own, short-lived (`HubPrincipalDelegation`, 60 seconds) — it does not forward what it was itself
given, so nothing downstream depends on the shape or the age of what a device or a browser holds.

**The secret is symmetric and shared**, not a key pair: every service that authenticates a caller, or
calls another on a caller's behalf, is given the same `FACTOTUM_HUB_TOKEN_SECRET`
(`factotum.common.hubauth`). One value to operate, not a key pair and a distribution problem — and
with a plain HMAC there is no cheaper way to let more than one party mint without also letting it
verify. A deployment that wants only the hub able to mint needs asymmetric signatures instead, which
is not done here.

**A token says nothing about when it was minted beyond its own expiry**: no session row, no list of
what has been issued, no identifier to revoke by. Checking one is pure computation. The price is that
nothing can be revoked before it expires, so the expiry itself is what bounds a leaked or unwanted
token: short for what crosses a service boundary (60 seconds), longer for what a person is handed at
login (`FACTOTUM_HUB_SESSION_HOURS`, 12 by default).

**Every service still falls back to the development authenticator** when no secret is configured,
which itself refuses to exist outside development and test: a production deployment that forgets to
set the secret still does not start, exactly as before the hub existed. The hub has no such fallback,
because it is the one thing with nothing left to fall back to.

## Consequences

- **Compromising any one service's secret lets an attacker mint a token for any tenant, any user, any
  role.** This is the same blast radius the development headers already had for anyone who could
  reach a service directly — now closed to whoever is outside the deployment, not closed between the
  services inside it. A deployment that wants a service unable to impersonate another needs
  asymmetric signing, a larger change than this slice makes.
- **No session list, no signing out everywhere, no seeing who is logged in.** These need a
  server-side record of what was issued, which a stateless token deliberately does not keep. They
  wait for a web app to show them in.
- **A device's long-lived credential (slice 7b) is a different problem from a session's.** A browser
  can afford a short token refreshed often; a speaker that is off for a week cannot ask the hub again
  before an expiry that short. Slice 7b decides what a device holds; nothing here commits to an
  answer for it.
- **An expiry is checked against the service that is checking it, not the hub's clock.** A service
  whose clock runs ahead treats a token as expiring sooner than the hub intended; one that runs
  behind treats it as lasting longer. Keeping every service's clock in sync, which a deployment
  already wants for its logs to agree, is relied on here and not separately enforced.
