"""Out-of-band approval, until there is a way to give one (ADR 0007).

Which mechanism asks an authorized person (an email, a one-time code, a push...) is not decided.
Until one is configured the platform uses the approver below, which says right away that there is
none, and an action that needs an approval is then refused with a plain reason: a risky action
cannot run by mistake because a mechanism is missing.
"""

from factotum.contracts.approval import ApprovalRequest, ApprovalResult


class NullApprover:
    """The approver of a deployment that has no way to ask: it is never available."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        return ApprovalResult.unavailable()
