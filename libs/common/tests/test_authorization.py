from collections.abc import Callable
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.common.authorization import authorize, can_access
from factotum.contracts.authorization import ActionRequest, DecisionOutcome, Reason
from factotum.contracts.devices import DevicePolicy
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, OwnerKind, Principal, Subject, SubjectKind
from factotum.contracts.ids import DeviceId, SkillId, TenantId, UserId
from factotum.contracts.skills import ApprovalPolicy, ConfirmPolicy
from factotum.contracts.testing import (
    make_device_id,
    make_skill_ref,
    make_target_ref,
    make_tenant_id,
    make_tenant_principal,
    make_user_id,
    make_user_principal,
)

R = Reason
D = DecisionOutcome


def tenant_owner(tenant_id: TenantId) -> Owner:
    return Owner.of_subject(Subject.tenant(tenant_id))


def user_owner(user_id: UserId) -> Owner:
    return Owner(kind=OwnerKind.USER, id=user_id)


class TestCanAccess:
    """The two subjects follow one procedure; only what they can see differs."""

    def test_a_tenant_owned_resource_is_available_to_a_user_and_to_the_tenant(self) -> None:
        tenant_id = make_tenant_id()
        owner = tenant_owner(tenant_id)

        for principal in (
            make_user_principal(tenant_id=tenant_id),
            make_tenant_principal(tenant_id=tenant_id),
        ):
            assert can_access(principal, resource_tenant_id=tenant_id, owner=owner)

    def test_a_user_owned_resource_is_available_only_to_that_user(self) -> None:
        tenant_id = make_tenant_id()
        owner_id = make_user_id()
        owner = user_owner(owner_id)

        the_owner = make_user_principal(tenant_id=tenant_id, user_id=owner_id)
        someone_else = make_user_principal(tenant_id=tenant_id)
        the_tenant = make_tenant_principal(tenant_id=tenant_id)

        assert can_access(the_owner, resource_tenant_id=tenant_id, owner=owner)
        assert not can_access(someone_else, resource_tenant_id=tenant_id, owner=owner)
        assert not can_access(the_tenant, resource_tenant_id=tenant_id, owner=owner)

    @pytest.mark.parametrize("make_principal", [make_user_principal, make_tenant_principal])
    def test_nothing_crosses_a_tenant_boundary(
        self, make_principal: Callable[..., Principal]
    ) -> None:
        principal = make_principal()
        other_tenant = make_tenant_id()

        assert not can_access(
            principal, resource_tenant_id=other_tenant, owner=tenant_owner(other_tenant)
        )

    def test_a_tenant_owner_must_be_the_principals_own_tenant(self) -> None:
        principal = make_tenant_principal()

        assert not can_access(
            principal, resource_tenant_id=principal.tenant_id, owner=tenant_owner(make_tenant_id())
        )

    def test_a_device_owned_resource_is_available_only_while_acting_on_that_device(self) -> None:
        principal = make_tenant_principal()
        tv = make_device_id()
        owner = Owner.of_device(tv)

        def check(via: DeviceId | None) -> bool:
            return can_access(
                principal,
                resource_tenant_id=principal.tenant_id,
                owner=owner,
                via_device=via,
            )

        assert check(tv)
        assert not check(None)
        assert not check(make_device_id())


class TestAuthorize:
    def setup_method(self) -> None:
        self.tenant_id = make_tenant_id()
        self.user_id = make_user_id()
        self.personal = make_user_principal(tenant_id=self.tenant_id, user_id=self.user_id)
        self.communal = make_tenant_principal(tenant_id=self.tenant_id)
        self.personal_policy = DevicePolicy.default_for(SubjectKind.USER)
        self.communal_policy = DevicePolicy.default_for(SubjectKind.TENANT)

    def request(
        self,
        *,
        effect: Effect = Effect.READ_ONLY,
        confirm: ConfirmPolicy = ConfirmPolicy.NEVER,
        approval: ApprovalPolicy = ApprovalPolicy.NONE,
        sensitivities: tuple[Effect, ...] = (),
        untrusted: bool = False,
    ) -> ActionRequest:
        return ActionRequest(
            skill=make_skill_ref(
                tenant_id=self.tenant_id, effect=effect, confirm=confirm, approval=approval
            ),
            targets=tuple(
                make_target_ref(tenant_id=self.tenant_id, sensitivity=s) for s in sensitivities
            ),
            untrusted_content_in_turn=untrusted,
        )

    # --- the easy case -------------------------------------------------------------------------

    def test_a_harmless_action_is_allowed(self) -> None:
        decision = authorize(self.personal, self.personal_policy, self.request())

        assert decision.outcome is D.ALLOW
        assert decision.reasons == ()
        assert decision.effective_effect is Effect.READ_ONLY

    def test_without_a_device_policy_there_is_no_device_restriction(self) -> None:
        request = self.request(effect=Effect.IRREVERSIBLE, confirm=ConfirmPolicy.NEVER)

        decision = authorize(self.personal, None, request)

        assert decision.outcome is D.REQUIRE_CONFIRMATION
        assert R.EXCEEDS_DEVICE_MAX_EFFECT not in decision.reasons

    # --- visibility ----------------------------------------------------------------------------

    def test_a_skill_owned_by_another_user_is_not_visible(self) -> None:
        skill = make_skill_ref(tenant_id=self.tenant_id, owner=user_owner(make_user_id()))

        decision = authorize(self.personal, self.personal_policy, ActionRequest(skill=skill))

        assert decision.outcome is D.DENY
        assert decision.reasons == (R.SKILL_NOT_VISIBLE,)

    def test_the_users_own_skill_is_visible_to_the_user_but_not_to_the_tenant(self) -> None:
        skill = make_skill_ref(tenant_id=self.tenant_id, owner=user_owner(self.user_id))
        request = ActionRequest(skill=skill)

        assert authorize(self.personal, self.personal_policy, request).outcome is D.ALLOW
        assert authorize(self.communal, self.communal_policy, request).reasons == (
            R.SKILL_NOT_VISIBLE,
        )

    def test_a_personal_device_of_another_user_is_not_a_visible_target(self) -> None:
        target = make_target_ref(tenant_id=self.tenant_id, owner=user_owner(make_user_id()))
        request = ActionRequest(skill=make_skill_ref(tenant_id=self.tenant_id), targets=(target,))

        decision = authorize(self.personal, self.personal_policy, request)

        assert decision.outcome is D.DENY
        assert decision.reasons == (R.TARGET_NOT_VISIBLE,)

    def test_a_communal_device_cannot_reach_a_personal_device(self) -> None:
        target = make_target_ref(tenant_id=self.tenant_id, owner=user_owner(self.user_id))
        request = ActionRequest(skill=make_skill_ref(tenant_id=self.tenant_id), targets=(target,))

        decision = authorize(self.communal, self.communal_policy, request)

        assert decision.reasons == (R.TARGET_NOT_VISIBLE,)

    def test_a_skill_or_target_of_another_tenant_is_reported_as_cross_tenant_only(self) -> None:
        other = make_tenant_id()
        request = ActionRequest(
            skill=make_skill_ref(tenant_id=other),
            targets=(make_target_ref(tenant_id=other), make_target_ref(tenant_id=other)),
        )

        decision = authorize(self.personal, self.personal_policy, request)

        assert decision.outcome is D.DENY
        assert decision.reasons == (R.CROSS_TENANT,)

    # --- the device's policy -------------------------------------------------------------------

    def test_a_communal_device_may_not_trigger_an_irreversible_action(self) -> None:
        decision = authorize(
            self.communal, self.communal_policy, self.request(effect=Effect.IRREVERSIBLE)
        )

        assert decision.outcome is D.DENY
        assert decision.reasons == (R.EXCEEDS_DEVICE_MAX_EFFECT,)

    def test_a_sensitive_target_raises_the_effect_of_a_harmless_skill(self) -> None:
        # A generic on/off skill: reversible on a lamp, irreversible on a garage door.
        request = self.request(effect=Effect.REVERSIBLE, sensitivities=(Effect.IRREVERSIBLE,))

        on_communal = authorize(self.communal, self.communal_policy, request)
        on_personal = authorize(self.personal, self.personal_policy, request)

        assert on_communal.effective_effect is Effect.IRREVERSIBLE
        assert on_communal.outcome is D.DENY
        assert on_personal.outcome is D.REQUIRE_CONFIRMATION
        assert on_personal.reasons == (R.IRREVERSIBLE_EFFECT,)

    def test_the_admin_can_raise_what_a_communal_device_may_do(self) -> None:
        policy = DevicePolicy(max_effect=Effect.IRREVERSIBLE)

        decision = authorize(self.communal, policy, self.request(effect=Effect.IRREVERSIBLE))

        assert decision.outcome is D.REQUIRE_CONFIRMATION

    def test_a_device_can_restrict_the_skills_it_may_use(self) -> None:
        request = self.request()
        allowed_elsewhere = DevicePolicy(
            max_effect=Effect.IRREVERSIBLE, allowed_skills=frozenset({SkillId(uuid4())})
        )
        allowed_here = DevicePolicy(
            max_effect=Effect.IRREVERSIBLE, allowed_skills=frozenset({request.skill.id})
        )

        assert authorize(self.personal, allowed_elsewhere, request).reasons == (
            R.SKILL_NOT_ALLOWED_ON_DEVICE,
        )
        assert authorize(self.personal, allowed_here, request).outcome is D.ALLOW

    # --- confirmation --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("effect", "confirm", "needs_confirmation"),
        [
            (Effect.READ_ONLY, ConfirmPolicy.NEVER, False),
            (Effect.REVERSIBLE, ConfirmPolicy.NEVER, False),
            (Effect.READ_ONLY, ConfirmPolicy.ASK, False),
            (Effect.REVERSIBLE, ConfirmPolicy.ASK, True),
            (Effect.READ_ONLY, ConfirmPolicy.ALWAYS, True),
            (Effect.REVERSIBLE, ConfirmPolicy.ALWAYS, True),
        ],
    )
    def test_the_skills_confirm_policy(
        self, effect: Effect, confirm: ConfirmPolicy, needs_confirmation: bool
    ) -> None:
        decision = authorize(
            self.personal, self.personal_policy, self.request(effect=effect, confirm=confirm)
        )

        assert (decision.outcome is D.REQUIRE_CONFIRMATION) is needs_confirmation
        if needs_confirmation:
            assert decision.reasons == (R.SKILL_CONFIRM_POLICY,)

    def test_an_irreversible_action_is_always_confirmed_even_if_the_skill_says_never(self) -> None:
        decision = authorize(
            self.personal,
            self.personal_policy,
            self.request(effect=Effect.IRREVERSIBLE, confirm=ConfirmPolicy.NEVER),
        )

        assert decision.outcome is D.REQUIRE_CONFIRMATION
        assert decision.reasons == (R.IRREVERSIBLE_EFFECT,)

    def test_untrusted_content_in_the_turn_forces_a_confirmation_for_actions_with_effects(
        self,
    ) -> None:
        decision = authorize(
            self.personal,
            self.personal_policy,
            self.request(effect=Effect.REVERSIBLE, untrusted=True),
        )

        assert decision.outcome is D.REQUIRE_CONFIRMATION
        assert decision.reasons == (R.UNTRUSTED_CONTENT_IN_TURN,)

    def test_untrusted_content_does_not_matter_for_a_read_only_action(self) -> None:
        decision = authorize(
            self.personal,
            self.personal_policy,
            self.request(effect=Effect.READ_ONLY, untrusted=True),
        )

        assert decision.outcome is D.ALLOW

    def test_every_reason_for_confirming_is_listed_once(self) -> None:
        decision = authorize(
            self.personal,
            self.personal_policy,
            self.request(effect=Effect.IRREVERSIBLE, confirm=ConfirmPolicy.ALWAYS, untrusted=True),
        )

        assert decision.reasons == (
            R.IRREVERSIBLE_EFFECT,
            R.SKILL_CONFIRM_POLICY,
            R.UNTRUSTED_CONTENT_IN_TURN,
        )

    # --- approval ------------------------------------------------------------------------------

    def test_a_skill_can_require_an_approval(self) -> None:
        decision = authorize(
            self.personal, self.personal_policy, self.request(approval=ApprovalPolicy.REQUIRED)
        )

        assert decision.outcome is D.REQUIRE_APPROVAL
        assert decision.reasons == (R.APPROVAL_REQUIRED_BY_SKILL,)

    def test_a_device_can_require_an_approval_for_everything(self) -> None:
        policy = DevicePolicy(max_effect=Effect.IRREVERSIBLE, requires_approval=True)

        decision = authorize(self.personal, policy, self.request())

        assert decision.outcome is D.REQUIRE_APPROVAL
        assert decision.reasons == (R.APPROVAL_REQUIRED_BY_DEVICE,)

    def test_an_approval_decision_also_lists_the_reasons_for_confirming(self) -> None:
        decision = authorize(
            self.personal,
            self.personal_policy,
            self.request(effect=Effect.IRREVERSIBLE, approval=ApprovalPolicy.REQUIRED),
        )

        assert decision.outcome is D.REQUIRE_APPROVAL
        assert decision.reasons == (R.APPROVAL_REQUIRED_BY_SKILL, R.IRREVERSIBLE_EFFECT)

    def test_a_denial_wins_over_an_approval(self) -> None:
        decision = authorize(
            self.communal,
            self.communal_policy,
            self.request(effect=Effect.IRREVERSIBLE, approval=ApprovalPolicy.REQUIRED),
        )

        assert decision.outcome is D.DENY


class TestOneProcedureForBothSubjects:
    """A device acting for a user and one acting for the tenant are judged the same way."""

    @pytest.mark.parametrize("effect", list(Effect))
    @pytest.mark.parametrize("confirm", list(ConfirmPolicy))
    @pytest.mark.parametrize("approval", list(ApprovalPolicy))
    def test_on_tenant_owned_resources_the_decisions_are_identical(
        self, effect: Effect, confirm: ConfirmPolicy, approval: ApprovalPolicy
    ) -> None:
        tenant_id = make_tenant_id()
        policy = DevicePolicy(max_effect=Effect.IRREVERSIBLE)
        request = ActionRequest(
            skill=make_skill_ref(
                tenant_id=tenant_id, effect=effect, confirm=confirm, approval=approval
            ),
            targets=(make_target_ref(tenant_id=tenant_id, sensitivity=Effect.REVERSIBLE),),
        )

        as_user = authorize(make_user_principal(tenant_id=tenant_id), policy, request)
        as_tenant = authorize(make_tenant_principal(tenant_id=tenant_id), policy, request)

        assert as_user == as_tenant


effects = st.sampled_from(list(Effect))


@given(
    skill_effect=effects,
    sensitivities=st.lists(effects, max_size=3),
    max_effect=effects,
    confirm=st.sampled_from(list(ConfirmPolicy)),
    approval=st.sampled_from(list(ApprovalPolicy)),
    device_requires_approval=st.booleans(),
    untrusted=st.booleans(),
    acts_as_tenant=st.booleans(),
)
def test_properties_of_the_decision(
    skill_effect: Effect,
    sensitivities: list[Effect],
    max_effect: Effect,
    confirm: ConfirmPolicy,
    approval: ApprovalPolicy,
    device_requires_approval: bool,
    untrusted: bool,
    acts_as_tenant: bool,
) -> None:
    tenant_id = make_tenant_id()
    principal = (
        make_tenant_principal(tenant_id=tenant_id)
        if acts_as_tenant
        else make_user_principal(tenant_id=tenant_id)
    )
    policy = DevicePolicy(max_effect=max_effect, requires_approval=device_requires_approval)
    request = ActionRequest(
        skill=make_skill_ref(
            tenant_id=tenant_id, effect=skill_effect, confirm=confirm, approval=approval
        ),
        targets=tuple(make_target_ref(tenant_id=tenant_id, sensitivity=s) for s in sensitivities),
        untrusted_content_in_turn=untrusted,
    )

    decision = authorize(principal, policy, request)

    effective = Effect.highest(skill_effect, *sensitivities)
    assert decision.effective_effect is effective
    if effective.exceeds(max_effect):
        # Never beyond what the device is allowed to trigger.
        assert decision.outcome is D.DENY
        assert R.EXCEEDS_DEVICE_MAX_EFFECT in decision.reasons
    else:
        wants_approval = approval is ApprovalPolicy.REQUIRED or device_requires_approval
        assert (decision.outcome is D.REQUIRE_APPROVAL) is wants_approval
    if decision.allowed:
        assert decision.reasons == ()
        assert effective is not Effect.IRREVERSIBLE
    assert len(set(decision.reasons)) == len(decision.reasons)


@given(skill_effect=effects, acts_as_tenant=st.booleans(), targets=st.integers(0, 3))
def test_a_resource_of_another_tenant_is_always_denied(
    skill_effect: Effect, acts_as_tenant: bool, targets: int
) -> None:
    mine, theirs = make_tenant_id(), make_tenant_id()
    principal = (
        make_tenant_principal(tenant_id=mine)
        if acts_as_tenant
        else make_user_principal(tenant_id=mine)
    )
    request = ActionRequest(
        skill=make_skill_ref(tenant_id=mine, effect=skill_effect),
        targets=tuple(make_target_ref(tenant_id=theirs) for _ in range(targets)),
    )

    decision = authorize(principal, DevicePolicy(max_effect=Effect.IRREVERSIBLE), request)

    assert (decision.outcome is D.DENY) is (targets > 0)
    if targets:
        assert R.CROSS_TENANT in decision.reasons
