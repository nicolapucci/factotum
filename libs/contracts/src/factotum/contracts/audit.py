"""The audit trail: who did what, through which device, and on whose behalf.

Accountability comes from the device and the subject the device acted for, and, when an approval
took place, from who approved. It never comes from recognizing a voice. Parameters are stored
already redacted.
"""

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from factotum.contracts.approval import ApprovalOutcome
from factotum.contracts.authorization import DecisionOutcome, Reason
from factotum.contracts.base import Contract
from factotum.contracts.identity import Subject
from factotum.contracts.ids import AuditId, DeviceId, SkillId, TenantId, UserId
from factotum.contracts.skills import ParamValue


class ActionResult(StrEnum):
    EXECUTED = "executed"
    FAILED = "failed"
    """The action was attempted and did not succeed."""

    NOT_EXECUTED = "not_executed"
    """Refused, declined or never started."""


class ApprovalTrail(Contract):
    outcome: ApprovalOutcome
    approved_by: UserId | None = None
    requested_at: AwareDatetime
    resolved_at: AwareDatetime

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        approved = self.outcome is ApprovalOutcome.APPROVED
        if approved and self.approved_by is None:
            raise ValueError("an approved request must say who approved it")
        if not approved and self.approved_by is not None:
            raise ValueError("only an approved request has an approver")
        if self.resolved_at < self.requested_at:
            raise ValueError("resolved_at cannot precede requested_at")
        return self


class AuditRecord(Contract):
    id: AuditId
    tenant_id: TenantId
    occurred_at: AwareDatetime
    device_id: DeviceId | None = None
    acts_as: Subject
    skill_id: SkillId
    skill_name: str = Field(min_length=1, max_length=64)
    targets: tuple[DeviceId, ...] = ()
    params: dict[str, ParamValue] = Field(default_factory=dict)
    decision: DecisionOutcome
    reasons: tuple[Reason, ...] = ()
    confirmed: bool | None = None
    """The answer to a confirmation, or None when none was asked."""

    approval: ApprovalTrail | None = None
    result: ActionResult
    detail: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _an_action_never_ran_without_the_checks_its_decision_required(self) -> Self:
        if self.result is ActionResult.NOT_EXECUTED:
            return self
        if self.decision is DecisionOutcome.DENY:
            raise ValueError("a denied action cannot have been attempted")
        if self.decision is DecisionOutcome.REQUIRE_CONFIRMATION and self.confirmed is not True:
            raise ValueError("an action that required confirmation ran without being confirmed")
        if self.decision is DecisionOutcome.REQUIRE_APPROVAL and not (
            self.approval is not None and self.approval.outcome is ApprovalOutcome.APPROVED
        ):
            raise ValueError("an action that required approval ran without being approved")
        return self
