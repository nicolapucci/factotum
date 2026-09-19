# 0004. Permissions never depend on a voice

Status: accepted, 2026-09-19

## Context

Recognizing who is speaking looks like an obvious way to know whose permissions apply. But a voice
varies with a cold, tiredness or noise, so a permission gate built on it would block people who are
entitled to use the system. Voice recognition also identifies a person from biometric data, which has
privacy consequences, in a company even for the monitoring of employees.

## Decision

Speaker recognition is not part of the design. A permission decision uses only whom the device acts
for, the device's policy, and the properties of the skill and its targets. Accountability comes from
the audit trail: the device, the subject it acted for and, when an approval took place, who approved.

Using a personal device therefore means acting with its owner's permissions; anyone near it can. That
risk is accepted, and the defences do not depend on a voice: a confirmation for irreversible actions
and after untrusted content, a per-device cap on the effect of actions, the sensitivity of the
target, optional out-of-band approval (ADR 0007), and administration only through the hub.

## Consequences

- The audit record and the principal have no speaker field, and tests check that a payload carrying
  one is rejected.
- If attribution by voice is ever wanted it can be added as an optional field for accountability. It
  must not become a permission factor.
