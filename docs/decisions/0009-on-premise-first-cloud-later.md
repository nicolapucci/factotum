# 0009. On-premise first, cloud later

Status: accepted, 2026-09-19

## Context

The target is a household or an organization running Factotum on its own machines, with Docker
Compose. A cloud deployment, with tenants that register and Kubernetes, should stay possible without
a rewrite.

## Decision

Build for on-premise now and keep the code multi-tenant capable. Services are stateless, configured
from the environment, and keep state only in Postgres and, later, a blob store. `tenant_id` is
everywhere. The environment defaults to `production`, so forgetting to configure it turns developer
conveniences off. Kubernetes packaging comes from the same configuration schema when the cloud becomes
a goal.

Before a multi-tenant cloud is offered, these gates must be met:

- a stronger skill sandbox (gVisor or Kata through a `RuntimeClass`), or only instructions-mode skills;
- stricter provider address rules: no `local` bindings, since a cloud cannot reach a private LAN
  (a hybrid with a cloud control plane and an on-premise data plane is a possible future);
- a `DeviceRouter` over a message bus, so a command reaches the edge replica that holds the device;
- tenant partitioning of vectors and iterative index scans;
- per-tenant quotas, keys and observability, and onboarding.

## Consequences

- Extra care now (tenant ids, row-level security, tests with two tenants) buys flexibility later.
- The cloud gates are listed in advance so they are not discovered late.
