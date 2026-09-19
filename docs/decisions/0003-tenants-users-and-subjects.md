# 0003. Tenants, users and subjects

Status: accepted, 2026-09-19

## Context

A deployment can be a household or an organization. In both, people have personal devices and there
are communal devices (a TV, a microphone in a shared room), and the same person or the community may
own the accounts linked to a service. The same code should run on-premise, with one tenant, and, one
day, in the cloud with one tenant per organization.

## Decision

- A **tenant** is the isolation boundary and is separate from the **user**. A user belongs to exactly
  one tenant in v1 and has the role `admin` or `member`. A tenant is created together with its admin
  and always has at least one.
- A **subject** is a user or the tenant. The tenant has the same standing as a user: it can own
  resources (skills, linked accounts, collections, model bindings) and permissions follow one
  procedure for both.
- A device **acts on behalf of its owner**: a personal device as its owner user, a communal device as
  the tenant. Shared spaces need no authentication, only tracing.
- A resource has an **owner**: the tenant (shared), a user (private) or a device (available only
  while acting on that device).
- `tenant_id` always comes from the authenticated token, never from a request body. On-premise is
  `single_tenant`; the code paths are the same in `multi_tenant`.
- Administration goes only through authenticated hub sessions, never through a device.

## Consequences

- `authorize()` and `can_access()` are pure functions, and their tests run the same scenarios for a
  user and for the tenant.
- Every tenant-owned table will carry `tenant_id` with row-level security, and every repository and
  API gets tests with two tenants.
- A person cannot belong to two organizations yet; membership can be added later without changing the
  meaning of a subject.
