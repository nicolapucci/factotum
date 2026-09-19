from uuid import uuid4

import pytest

from factotum.contracts.authorization import (
    ActionRequest,
    Decision,
    DecisionOutcome,
    SkillRef,
    TargetRef,
)
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import SkillId
from factotum.contracts.skills import ApprovalPolicy, ConfirmPolicy, SkillManifest
from factotum.contracts.testing import (
    make_external_device,
    make_skill_ref,
    make_system_device,
    make_tenant_id,
)


def test_a_skill_ref_takes_from_the_manifest_what_the_authorization_needs() -> None:
    manifest = SkillManifest.model_validate(
        {
            "name": "open-garage",
            "description": "Open the garage door.",
            "effect": "irreversible",
            "confirm": "ask",
            "approval": "required",
        }
    )
    tenant_id = make_tenant_id()
    owner = Owner.of_subject(Subject.tenant(tenant_id))
    skill_id = SkillId(uuid4())

    ref = SkillRef.from_manifest(manifest, skill_id=skill_id, tenant_id=tenant_id, owner=owner)

    assert ref.id == skill_id
    assert ref.effect is Effect.IRREVERSIBLE
    assert ref.confirm is ConfirmPolicy.ASK
    assert ref.approval is ApprovalPolicy.REQUIRED


def test_a_target_ref_takes_the_sensitivity_of_an_external_device() -> None:
    garage_door = make_external_device(sensitivity=Effect.IRREVERSIBLE)

    ref = TargetRef.from_record(garage_door)

    assert ref.device_id == garage_door.id
    assert ref.tenant_id == garage_door.tenant_id
    assert ref.owner == garage_door.owner
    assert ref.sensitivity is Effect.IRREVERSIBLE


def test_a_system_device_adds_no_sensitivity() -> None:
    assert TargetRef.from_record(make_system_device()).sensitivity is Effect.READ_ONLY


def test_an_action_request_aims_at_nothing_and_trusts_the_turn_by_default() -> None:
    request = ActionRequest(skill=make_skill_ref(tenant_id=make_tenant_id()))

    assert request.targets == ()
    assert request.untrusted_content_in_turn is False


@pytest.mark.parametrize("outcome", list(DecisionOutcome))
def test_a_decision_is_allowed_only_when_the_outcome_is_allow(outcome: DecisionOutcome) -> None:
    decision = Decision(outcome=outcome, effective_effect=Effect.READ_ONLY)

    assert decision.allowed is (outcome is DecisionOutcome.ALLOW)
