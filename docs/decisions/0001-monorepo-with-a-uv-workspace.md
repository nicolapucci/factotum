# 0001. Monorepo with a uv workspace

Status: accepted, 2026-09-19

## Context

Several services and clients talk to each other through shared shapes. In the prototype that
preceded Factotum the interfaces were duplicated across independent folders and drifted apart: one
service sent a JSON body that another read as a query string, and the answer had a different type
from the one expected. Nobody noticed because nothing exercised the two sides together.

## Decision

One repository. Libraries and, later, services and clients are members of a `uv` workspace that shares
one lockfile and one set of tools (ruff, mypy, pytest) configured at the root. Every member belongs
to the PEP 420 namespace `factotum` (`factotum.contracts`, `factotum.common`, `factotum.devices`,
`factotum.model_gateway`...). The shared interfaces live in `factotum.contracts`.

## Consequences

- A change to a contract is seen by every member's tests at once, and the type checker fails on the
  side that was not updated.
- Docker images will be built with the repository root as context, copying `libs/` and the service.
- Namespace packages need `explicit_package_bases` in mypy; it is configured, and a test keeps the
  libraries importable side by side.
- Tests across libraries must have unique file names, because mypy checks them in one run.
