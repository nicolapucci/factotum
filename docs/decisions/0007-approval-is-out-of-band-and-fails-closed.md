# 0007. Approval is out of band and fails closed

Status: accepted, 2026-09-19

## Context

Some operations are risky enough to need the go-ahead of an authorized person, given through a channel
other than the conversation. Which mechanism to use is undecided: an email to the owner, a one-time
code spoken to the speaker, a push notification, something else. A spoken "yes" is a useful safeguard
against misunderstandings but it authenticates nobody.

## Decision

An `Approver` interface, `request_approval(ApprovalRequest) -> ApprovalOutcome`, with the outcomes
`approved`, `denied`, `expired` and `unavailable`. The request says who may approve: the owner user
for a personal device, the tenant's admins for a communal one.

Approval is opt-in: a skill asks for it in its manifest, or a device's policy does. Until a mechanism
is configured the platform uses an approver that answers `unavailable`, and an action that requires
approval is refused with an explicit reason.

The spoken yes/no confirmation stays as a separate, lighter step.

## Consequences

- An action marked as risky cannot run by mistake because a mechanism is missing.
- The audit record stores the approval trail (outcome, who approved, when) and refuses to describe an
  action that ran without a granted approval.
- The signature may grow once a mechanism is chosen: a spoken one-time code needs a channel back to
  the conversation, an email needs an approval endpoint in the hub.
- Which actions require approval by default is still open.
