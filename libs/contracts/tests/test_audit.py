from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from factotum.contracts.approval import ApprovalOutcome
from factotum.contracts.audit import ActionResult, ApprovalTrail, AuditRecord
from factotum.contracts.authorization import DecisionOutcome, Reason
from factotum.contracts.identity import Subject
from factotum.contracts.ids import AuditId, SkillId
from factotum.contracts.testing import make_device_id, make_tenant_id, make_user_id

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def trail(outcome: ApprovalOutcome = ApprovalOutcome.APPROVED, **overrides: Any) -> ApprovalTrail:
    fields: dict[str, Any] = {
        "outcome": outcome,
        "approved_by": make_user_id() if outcome is ApprovalOutcome.APPROVED else None,
        "requested_at": NOW,
        "resolved_at": NOW + timedelta(seconds=30),
    }
    return ApprovalTrail(**(fields | overrides))


def record(**overrides: Any) -> AuditRecord:
    tenant_id = make_tenant_id()
    fields: dict[str, Any] = {
        "id": AuditId(uuid4()),
        "tenant_id": tenant_id,
        "occurred_at": NOW,
        "device_id": make_device_id(),
        "acts_as": Subject.tenant(tenant_id),
        "skill_id": SkillId(uuid4()),
        "skill_name": "living-room-lights",
        "targets": (make_device_id(),),
        "params": {"state": "on"},
        "decision": DecisionOutcome.ALLOW,
        "result": ActionResult.EXECUTED,
    }
    return AuditRecord(**(fields | overrides))


class TestApprovalTrail:
    def test_an_approved_request_must_say_who_approved_it(self) -> None:
        with pytest.raises(ValidationError, match="must say who approved"):
            trail(ApprovalOutcome.APPROVED, approved_by=None)

    @pytest.mark.parametrize(
        "outcome",
        [ApprovalOutcome.DENIED, ApprovalOutcome.EXPIRED, ApprovalOutcome.UNAVAILABLE],
    )
    def test_only_an_approved_request_has_an_approver(self, outcome: ApprovalOutcome) -> None:
        with pytest.raises(ValidationError, match="only an approved request"):
            trail(outcome, approved_by=make_user_id())

    def test_the_resolution_cannot_precede_the_request(self) -> None:
        with pytest.raises(ValidationError, match="cannot precede"):
            trail(resolved_at=NOW - timedelta(seconds=1))

    @pytest.mark.parametrize("outcome", list(ApprovalOutcome))
    def test_every_outcome_can_be_recorded(self, outcome: ApprovalOutcome) -> None:
        assert trail(outcome).outcome is outcome


class TestAuditRecord:
    def test_a_plain_allowed_action_is_recorded(self) -> None:
        entry = record()

        assert entry.result is ActionResult.EXECUTED
        assert entry.confirmed is None
        assert entry.approval is None

    def test_timestamps_must_carry_a_time_zone(self) -> None:
        with pytest.raises(ValidationError, match="timezone"):
            record(occurred_at=datetime(2026, 9, 19, 12, 0))  # noqa: DTZ001

    @pytest.mark.parametrize("result", [ActionResult.EXECUTED, ActionResult.FAILED])
    def test_a_denied_action_cannot_have_been_attempted(self, result: ActionResult) -> None:
        with pytest.raises(ValidationError, match="denied action cannot have been attempted"):
            record(decision=DecisionOutcome.DENY, result=result)

    def test_a_denied_action_is_recorded_as_not_executed(self) -> None:
        entry = record(
            decision=DecisionOutcome.DENY,
            reasons=(Reason.EXCEEDS_DEVICE_MAX_EFFECT,),
            result=ActionResult.NOT_EXECUTED,
        )

        assert entry.reasons == (Reason.EXCEEDS_DEVICE_MAX_EFFECT,)

    @pytest.mark.parametrize("confirmed", [None, False])
    def test_confirmation_must_have_been_given_before_the_action_ran(
        self, confirmed: bool | None
    ) -> None:
        with pytest.raises(ValidationError, match="without being confirmed"):
            record(decision=DecisionOutcome.REQUIRE_CONFIRMATION, confirmed=confirmed)

    def test_a_declined_confirmation_is_recorded_as_not_executed(self) -> None:
        entry = record(
            decision=DecisionOutcome.REQUIRE_CONFIRMATION,
            confirmed=False,
            result=ActionResult.NOT_EXECUTED,
        )

        assert entry.confirmed is False

    def test_a_confirmed_action_can_be_recorded_as_executed(self) -> None:
        entry = record(decision=DecisionOutcome.REQUIRE_CONFIRMATION, confirmed=True)

        assert entry.result is ActionResult.EXECUTED

    @pytest.mark.parametrize(
        "approval",
        [
            None,
            trail(ApprovalOutcome.DENIED),
            trail(ApprovalOutcome.EXPIRED),
            trail(ApprovalOutcome.UNAVAILABLE),
        ],
        ids=["never-requested", "denied", "expired", "unavailable"],
    )
    def test_approval_must_have_been_granted_before_the_action_ran(
        self, approval: ApprovalTrail | None
    ) -> None:
        with pytest.raises(ValidationError, match="without being approved"):
            record(decision=DecisionOutcome.REQUIRE_APPROVAL, approval=approval)

    def test_an_approved_action_is_recorded_with_who_approved_it(self) -> None:
        approver = make_user_id()
        approval = trail(ApprovalOutcome.APPROVED, approved_by=approver)

        entry = record(decision=DecisionOutcome.REQUIRE_APPROVAL, approval=approval)

        assert entry.approval is not None
        assert entry.approval.approved_by == approver

    def test_a_refused_approval_is_recorded_as_not_executed(self) -> None:
        entry = record(
            decision=DecisionOutcome.REQUIRE_APPROVAL,
            approval=trail(ApprovalOutcome.UNAVAILABLE),
            result=ActionResult.NOT_EXECUTED,
        )

        assert entry.approval is not None
        assert entry.approval.outcome is ApprovalOutcome.UNAVAILABLE

    def test_a_record_has_no_speaker_field(self) -> None:
        payload = record().model_dump(mode="json") | {"speaker": "someone"}

        with pytest.raises(ValidationError, match="speaker"):
            AuditRecord.model_validate(payload)

    def test_a_record_round_trips_through_json(self) -> None:
        entry = record(
            decision=DecisionOutcome.REQUIRE_APPROVAL,
            reasons=(Reason.APPROVAL_REQUIRED_BY_SKILL,),
            approval=trail(),
        )

        assert AuditRecord.model_validate_json(entry.model_dump_json()) == entry
