from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from factotum.contracts.approval import (
    ActionSummary,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalResult,
    Approver,
    ApproverKind,
    ApproverSelector,
)
from factotum.contracts.authorization import Reason
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Subject
from factotum.contracts.ids import ApprovalId, SkillId
from factotum.contracts.testing import make_tenant_id, make_user_id

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class TestApproverSelector:
    def test_a_user_approver_needs_a_user_id(self) -> None:
        with pytest.raises(ValidationError, match="needs a user_id"):
            ApproverSelector(kind=ApproverKind.USER)

    def test_tenant_admins_are_not_a_single_user(self) -> None:
        with pytest.raises(ValidationError, match="not a single user"):
            ApproverSelector(kind=ApproverKind.TENANT_ADMINS, user_id=make_user_id())

    def test_the_owner_approves_for_a_personal_device(self) -> None:
        owner = make_user_id()

        selector = ApproverSelector.for_subject(Subject.user(owner))

        assert selector == ApproverSelector.user(owner)

    def test_the_admins_approve_for_a_communal_device(self) -> None:
        selector = ApproverSelector.for_subject(Subject.tenant(make_tenant_id()))

        assert selector == ApproverSelector.tenant_admins()


def make_request(**overrides: Any) -> ApprovalRequest:
    tenant_id = make_tenant_id()
    fields: dict[str, Any] = {
        "id": ApprovalId(uuid4()),
        "tenant_id": tenant_id,
        "acts_as": Subject.tenant(tenant_id),
        "action": ActionSummary(
            skill_id=SkillId(uuid4()), skill_name="open-garage", params={"door": "main"}
        ),
        "risk": Effect.IRREVERSIBLE,
        "reasons": (Reason.APPROVAL_REQUIRED_BY_SKILL,),
        "approvers": ApproverSelector.tenant_admins(),
        "requested_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    return ApprovalRequest(**(fields | overrides))


class TestApprovalRequest:
    def test_a_request_is_valid(self) -> None:
        request = make_request()

        assert request.risk is Effect.IRREVERSIBLE
        assert request.device_id is None

    def test_it_must_expire_after_it_was_requested(self) -> None:
        with pytest.raises(ValidationError, match="expires_at must be after requested_at"):
            make_request(expires_at=NOW)

    def test_timestamps_must_carry_a_time_zone(self) -> None:
        with pytest.raises(ValidationError, match="timezone"):
            make_request(requested_at=datetime(2026, 9, 19, 12, 0))  # noqa: DTZ001

    def test_a_request_round_trips_through_json(self) -> None:
        request = make_request()

        assert ApprovalRequest.model_validate_json(request.model_dump_json()) == request

    def test_the_action_summary_needs_a_skill_name(self) -> None:
        with pytest.raises(ValidationError):
            ActionSummary(skill_id=SkillId(uuid4()), skill_name="")


class RecordingApprover:
    """The smallest possible mechanism: anything with the right method is an Approver."""

    def __init__(self, result: ApprovalResult) -> None:
        self.result = result
        self.seen: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        self.seen.append(request)
        return self.result


@pytest.mark.parametrize("outcome", list(ApprovalOutcome))
async def test_an_approver_answers_with_one_of_the_outcomes(outcome: ApprovalOutcome) -> None:
    result = (
        ApprovalResult.approved(make_user_id())
        if outcome is ApprovalOutcome.APPROVED
        else ApprovalResult(outcome=outcome)
    )
    approver: Approver = RecordingApprover(result)
    request = make_request()

    assert await approver.request_approval(request) == result


class TestApprovalResult:
    def test_an_approval_says_who_approved(self) -> None:
        person = make_user_id()

        assert ApprovalResult.approved(person).approved_by == person
        with pytest.raises(ValidationError, match="must say who approved"):
            ApprovalResult(outcome=ApprovalOutcome.APPROVED)

    @pytest.mark.parametrize(
        "outcome",
        [ApprovalOutcome.DENIED, ApprovalOutcome.EXPIRED, ApprovalOutcome.UNAVAILABLE],
    )
    def test_nothing_else_has_an_approver(self, outcome: ApprovalOutcome) -> None:
        assert ApprovalResult(outcome=outcome).approved_by is None
        with pytest.raises(ValidationError, match="only an approval has an approver"):
            ApprovalResult(outcome=outcome, approved_by=make_user_id())

    def test_the_answer_of_a_mechanism_that_is_not_there_is_unavailable(self) -> None:
        assert ApprovalResult.unavailable().outcome is ApprovalOutcome.UNAVAILABLE
