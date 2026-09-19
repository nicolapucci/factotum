"""Authorization: the single procedure that decides whether an action may happen.

The procedure is the same whether a device acts for a user (a personal device) or for the tenant
(a communal device). Only the ownership rules change *what is visible* to each of them. The voice
of whoever is speaking plays no part, and neither does any role: through a device one *uses* the
system, one does not administer it (administration goes through authenticated hub sessions).

Both functions are pure, so every rule can be tested exhaustively.
"""

from collections.abc import Iterable

from factotum.contracts.authorization import ActionRequest, Decision, DecisionOutcome, Reason
from factotum.contracts.devices import DevicePolicy
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, OwnerKind, Principal, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId
from factotum.contracts.skills import ApprovalPolicy, ConfirmPolicy


def can_access(
    principal: Principal,
    *,
    resource_tenant_id: TenantId,
    owner: Owner,
    via_device: DeviceId | None = None,
) -> bool:
    """Whether the principal may see and use a resource.

    * Nothing crosses a tenant boundary.
    * A tenant-owned resource is available to every subject of that tenant.
    * A user-owned resource is available only to that user.
    * A device-owned resource (a TV's own streaming account) is available only while acting on
      that device, which is what ``via_device`` says.
    """
    if resource_tenant_id != principal.tenant_id:
        return False
    match owner.kind:
        case OwnerKind.TENANT:
            return owner.id == principal.tenant_id
        case OwnerKind.USER:
            return principal.acts_as == Subject.user(UserId(owner.id))
        case OwnerKind.DEVICE:
            return via_device is not None and owner.id == via_device


def authorize(
    principal: Principal, device_policy: DevicePolicy | None, action: ActionRequest
) -> Decision:
    """Decide what has to happen before ``action`` may run.

    ``device_policy`` is the policy of the device the request came from; ``None`` means there is
    no device restriction (a run started from an authenticated hub session).

    The outcome is ``DENY`` when the skill or a target is not visible to the principal, when the
    device's policy forbids the skill or the effect, ``REQUIRE_APPROVAL`` when a skill or the
    device asks for it, ``REQUIRE_CONFIRMATION`` when the effect, the skill's policy or untrusted
    content in the turn call for a plain yes or no, and ``ALLOW`` otherwise. A denial always wins.
    """
    effective = Effect.highest(
        action.skill.effect, *(target.sensitivity for target in action.targets)
    )

    denials = _unique(_denial_reasons(principal, device_policy, action, effective))
    if denials:
        return Decision(outcome=DecisionOutcome.DENY, effective_effect=effective, reasons=denials)

    approval = _approval_reasons(device_policy, action)
    confirmation = _confirmation_reasons(action, effective)
    if approval:
        return Decision(
            outcome=DecisionOutcome.REQUIRE_APPROVAL,
            effective_effect=effective,
            reasons=(*approval, *confirmation),
        )
    if confirmation:
        return Decision(
            outcome=DecisionOutcome.REQUIRE_CONFIRMATION,
            effective_effect=effective,
            reasons=confirmation,
        )
    return Decision(outcome=DecisionOutcome.ALLOW, effective_effect=effective)


def _unique(reasons: Iterable[Reason]) -> tuple[Reason, ...]:
    return tuple(dict.fromkeys(reasons))


def _visibility_reason(
    principal: Principal, tenant_id: TenantId, owner: Owner, not_visible: Reason
) -> Reason | None:
    if tenant_id != principal.tenant_id:
        return Reason.CROSS_TENANT
    if not can_access(principal, resource_tenant_id=tenant_id, owner=owner):
        return not_visible
    return None


def _denial_reasons(
    principal: Principal,
    device_policy: DevicePolicy | None,
    action: ActionRequest,
    effective: Effect,
) -> Iterable[Reason]:
    skill = action.skill
    skill_reason = _visibility_reason(
        principal, skill.tenant_id, skill.owner, Reason.SKILL_NOT_VISIBLE
    )
    if skill_reason is not None:
        yield skill_reason

    for target in action.targets:
        target_reason = _visibility_reason(
            principal, target.tenant_id, target.owner, Reason.TARGET_NOT_VISIBLE
        )
        if target_reason is not None:
            yield target_reason

    if device_policy is not None:
        allowed = device_policy.allowed_skills
        if allowed is not None and skill.id not in allowed:
            yield Reason.SKILL_NOT_ALLOWED_ON_DEVICE
        if effective.exceeds(device_policy.max_effect):
            yield Reason.EXCEEDS_DEVICE_MAX_EFFECT


def _approval_reasons(
    device_policy: DevicePolicy | None, action: ActionRequest
) -> tuple[Reason, ...]:
    reasons: list[Reason] = []
    if action.skill.approval is ApprovalPolicy.REQUIRED:
        reasons.append(Reason.APPROVAL_REQUIRED_BY_SKILL)
    if device_policy is not None and device_policy.requires_approval:
        reasons.append(Reason.APPROVAL_REQUIRED_BY_DEVICE)
    return tuple(reasons)


def _confirmation_reasons(action: ActionRequest, effective: Effect) -> tuple[Reason, ...]:
    reasons: list[Reason] = []
    has_effect = effective.exceeds(Effect.READ_ONLY)
    if effective is Effect.IRREVERSIBLE:
        reasons.append(Reason.IRREVERSIBLE_EFFECT)
    confirm = action.skill.confirm
    if confirm is ConfirmPolicy.ALWAYS or (confirm is ConfirmPolicy.ASK and has_effect):
        reasons.append(Reason.SKILL_CONFIRM_POLICY)
    if action.untrusted_content_in_turn and has_effect:
        reasons.append(Reason.UNTRUSTED_CONTENT_IN_TURN)
    return tuple(reasons)
