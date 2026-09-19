"""The vocabulary of authorization: what is asked, and what is decided.

``factotum.common.authorization.authorize`` turns an :class:`ActionRequest` into a
:class:`Decision`. Nothing else takes part in the decision: not who the speaker seems to be, only
whom the device acts for, the device's policy and the properties of the skill and its targets.
"""

from enum import StrEnum
from typing import Self

from factotum.contracts.base import Contract
from factotum.contracts.devices import ExternalDeviceRecord, SystemDeviceRecord
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner
from factotum.contracts.ids import DeviceId, SkillId, TenantId
from factotum.contracts.skills import ApprovalPolicy, ConfirmPolicy, SkillManifest


class DecisionOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    """Ask the person for a plain yes or no. It authenticates nobody."""

    REQUIRE_APPROVAL = "require_approval"
    """Ask an authorized person, out of band, through an ``Approver``."""

    DENY = "deny"


class Reason(StrEnum):
    """Why a decision came out the way it did. Kept in the audit trail."""

    CROSS_TENANT = "cross_tenant"
    SKILL_NOT_VISIBLE = "skill_not_visible"
    TARGET_NOT_VISIBLE = "target_not_visible"
    SKILL_NOT_ALLOWED_ON_DEVICE = "skill_not_allowed_on_device"
    EXCEEDS_DEVICE_MAX_EFFECT = "exceeds_device_max_effect"
    APPROVAL_REQUIRED_BY_SKILL = "approval_required_by_skill"
    APPROVAL_REQUIRED_BY_DEVICE = "approval_required_by_device"
    IRREVERSIBLE_EFFECT = "irreversible_effect"
    SKILL_CONFIRM_POLICY = "skill_confirm_policy"
    UNTRUSTED_CONTENT_IN_TURN = "untrusted_content_in_turn"


class SkillRef(Contract):
    """What the authorization needs to know about a skill."""

    id: SkillId
    tenant_id: TenantId
    owner: Owner
    effect: Effect
    confirm: ConfirmPolicy = ConfirmPolicy.NEVER
    approval: ApprovalPolicy = ApprovalPolicy.NONE

    @classmethod
    def from_manifest(
        cls, manifest: SkillManifest, *, skill_id: SkillId, tenant_id: TenantId, owner: Owner
    ) -> Self:
        return cls(
            id=skill_id,
            tenant_id=tenant_id,
            owner=owner,
            effect=manifest.effect,
            confirm=manifest.confirm,
            approval=manifest.approval,
        )


class TargetRef(Contract):
    """What the authorization needs to know about a device an action is aimed at."""

    device_id: DeviceId
    tenant_id: TenantId
    owner: Owner
    sensitivity: Effect = Effect.READ_ONLY

    @classmethod
    def from_record(cls, record: SystemDeviceRecord | ExternalDeviceRecord) -> Self:
        sensitivity = (
            record.sensitivity if isinstance(record, ExternalDeviceRecord) else Effect.READ_ONLY
        )
        return cls(
            device_id=record.id,
            tenant_id=record.tenant_id,
            owner=record.owner,
            sensitivity=sensitivity,
        )


class ActionRequest(Contract):
    """An action somebody wants to perform, to be authorized before anything happens."""

    skill: SkillRef
    targets: tuple[TargetRef, ...] = ()
    untrusted_content_in_turn: bool = False
    """True when text from the web or from a document entered the conversation turn: nothing that
    has an effect may then run without a confirmation (a page must not be able to order actions)."""


class Decision(Contract):
    outcome: DecisionOutcome
    effective_effect: Effect
    """The most severe of the skill's effect and the sensitivity of its targets."""

    reasons: tuple[Reason, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.outcome is DecisionOutcome.ALLOW
