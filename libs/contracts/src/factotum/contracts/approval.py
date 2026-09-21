"""Out-of-band approval of risky operations.

Some operations are risky enough to need the go-ahead of an authorized person through a channel
other than the conversation (an email, a one-time code, a push notification...). Which mechanism
to use is still open, so this module only fixes the shape of the request and the outcome, and the
``Approver`` interface a mechanism will implement. Until one is configured the platform uses an
approver that answers ``UNAVAILABLE``, and an action that requires approval is refused.
"""

from enum import StrEnum
from typing import Protocol, Self

from pydantic import AwareDatetime, Field, model_validator

from factotum.contracts.authorization import Reason
from factotum.contracts.base import Contract
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Subject, SubjectKind
from factotum.contracts.ids import ApprovalId, DeviceId, SkillId, TenantId, UserId
from factotum.contracts.skills import ParamValue


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"
    """No mechanism is configured, or it could not be reached. Treated as a refusal."""


class ApprovalResult(Contract):
    """How an approval request came out and, when it was approved, who approved it: the audit trail
    says who did (ADR 0004), and only a mechanism that authenticated somebody can tell."""

    outcome: ApprovalOutcome
    approved_by: UserId | None = None

    @model_validator(mode="after")
    def _only_an_approval_has_an_approver(self) -> Self:
        approved = self.outcome is ApprovalOutcome.APPROVED
        if approved and self.approved_by is None:
            raise ValueError("an approval must say who approved")
        if not approved and self.approved_by is not None:
            raise ValueError("only an approval has an approver")
        return self

    @classmethod
    def approved(cls, by: UserId) -> Self:
        return cls(outcome=ApprovalOutcome.APPROVED, approved_by=by)

    @classmethod
    def unavailable(cls) -> Self:
        return cls(outcome=ApprovalOutcome.UNAVAILABLE)


class ApproverKind(StrEnum):
    USER = "user"
    TENANT_ADMINS = "tenant_admins"


class ApproverSelector(Contract):
    """Who may approve: a given user, or any admin of the tenant."""

    kind: ApproverKind
    user_id: UserId | None = None

    @model_validator(mode="after")
    def _matches_the_kind(self) -> Self:
        if self.kind is ApproverKind.USER and self.user_id is None:
            raise ValueError("a user approver needs a user_id")
        if self.kind is ApproverKind.TENANT_ADMINS and self.user_id is not None:
            raise ValueError("tenant admins are not a single user")
        return self

    @classmethod
    def user(cls, user_id: UserId) -> Self:
        return cls(kind=ApproverKind.USER, user_id=user_id)

    @classmethod
    def tenant_admins(cls) -> Self:
        return cls(kind=ApproverKind.TENANT_ADMINS)

    @classmethod
    def for_subject(cls, subject: Subject) -> Self:
        """The default: the owner approves for a personal device, the admins for a communal one."""
        if subject.kind is SubjectKind.USER:
            return cls.user(UserId(subject.id))
        return cls.tenant_admins()


class ActionSummary(Contract):
    """What the approver is asked to approve. Parameters are already redacted."""

    skill_id: SkillId
    skill_name: str = Field(min_length=1, max_length=64)
    targets: tuple[DeviceId, ...] = ()
    params: dict[str, ParamValue] = Field(default_factory=dict)


class ApprovalRequest(Contract):
    id: ApprovalId
    tenant_id: TenantId
    acts_as: Subject
    device_id: DeviceId | None = None
    action: ActionSummary
    risk: Effect
    reasons: tuple[Reason, ...] = ()
    approvers: ApproverSelector
    requested_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _expires_after_it_was_requested(self) -> Self:
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be after requested_at")
        return self


class Approver(Protocol):
    """A mechanism that asks an authorized person and reports the answer.

    The call returns when the request is approved (and by whom), denied or expired, or right away
    with ``UNAVAILABLE`` when the mechanism cannot be used. The signature may still grow once a
    mechanism is chosen (a spoken one-time code needs a channel back to the conversation).
    """

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResult: ...
