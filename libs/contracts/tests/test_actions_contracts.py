from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from factotum.contracts.actions import (
    MAX_REPLY_CHARS,
    ActCode,
    ActOutcome,
    ActRequest,
    ActStatus,
    AuditList,
    SkillDetail,
    SkillEnable,
    SkillInfo,
    SkillInstall,
    SkillList,
    SkillOwner,
    SkillReload,
    SkillVersionInfo,
)
from factotum.contracts.authorization import Reason
from factotum.contracts.commands import SetVolume
from factotum.contracts.conversation import MAX_MESSAGE_CHARS, Phase, WarningCode
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import AuditId, SkillId, TenantId
from factotum.contracts.runner import MAX_COMMANDS_PER_RUN, RunRequest, RunResult
from factotum.contracts.skills import SkillManifest, SkillMode, SkillPermissions, SkillTarget

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
TENANT = TenantId(uuid4())


def manifest() -> SkillManifest:
    return SkillManifest(
        name="lamp", description="Turns a lamp on or off.", effect=Effect.REVERSIBLE
    )


def info(**changes: object) -> SkillInfo:
    fields: dict[str, object] = {
        "id": SkillId(uuid4()),
        "tenant_id": TENANT,
        "owner": Owner.of_subject(Subject.tenant(TENANT)),
        "name": "lamp",
        "description": "Turns a lamp on or off.",
        "target": SkillTarget.EXTERNAL,
        "mode": SkillMode.SCRIPT,
        "effect": Effect.REVERSIBLE,
        "latest_version": 1,
    }
    return SkillInfo.model_validate({**fields, **changes})


class TestWhatAMessageCameTo:
    def test_a_message_that_asks_for_nothing_says_nothing(self) -> None:
        outcome = ActOutcome.no_action()

        assert (outcome.status, outcome.reply, outcome.handled) == (
            ActStatus.NO_ACTION,
            None,
            False,
        )

    def test_a_message_that_could_not_be_read_is_no_action_and_says_so(self) -> None:
        outcome = ActOutcome.not_read()

        assert (outcome.status, outcome.handled, outcome.degraded) == (
            ActStatus.NO_ACTION,
            False,
            True,
        )
        assert ActOutcome.no_action().degraded is False

    @pytest.mark.parametrize(
        "status", [status for status in ActStatus if status is not ActStatus.NO_ACTION]
    )
    def test_only_a_message_that_is_no_action_can_be_one_that_was_not_read(
        self, status: ActStatus
    ) -> None:
        with pytest.raises(ValidationError, match="only a message that is no action"):
            ActOutcome(status=status, reply="Okay.", degraded=True)

    @pytest.mark.parametrize(
        "status", [status for status in ActStatus if status is not ActStatus.NO_ACTION]
    )
    def test_every_other_outcome_has_something_to_say(self, status: ActStatus) -> None:
        assert ActOutcome(status=status, reply="Okay.").handled is True
        with pytest.raises(ValidationError, match="something to say"):
            ActOutcome(status=status)

    @pytest.mark.parametrize(
        "extra",
        [{"reply": "Okay."}, {"code": ActCode.DENIED}, {"skill": "lamp"}],
    )
    def test_a_message_that_is_no_action_cannot_be_given_anything_else(
        self, extra: dict[str, object]
    ) -> None:
        with pytest.raises(ValidationError, match="no action has no reply"):
            ActOutcome(status=ActStatus.NO_ACTION, **extra)  # type: ignore[arg-type]

    def test_an_outcome_says_why_and_what_it_left_behind(self) -> None:
        audit = AuditId(uuid4())

        outcome = ActOutcome(
            status=ActStatus.REFUSED,
            reply="This device may not do that.",
            code=ActCode.DENIED,
            skill="garage-door",
            reasons=(Reason.EXCEEDS_DEVICE_MAX_EFFECT,),
            audit_id=audit,
        )

        assert outcome.reasons == (Reason.EXCEEDS_DEVICE_MAX_EFFECT,)
        assert outcome.audit_id == audit

    def test_what_is_said_is_at_most_what_a_device_speaks_in_one_part(self) -> None:
        ActOutcome(status=ActStatus.DONE, reply="x" * MAX_REPLY_CHARS)
        with pytest.raises(ValidationError):
            ActOutcome(status=ActStatus.DONE, reply="x" * (MAX_REPLY_CHARS + 1))

    def test_an_outcome_rejects_what_it_does_not_know(self) -> None:
        with pytest.raises(ValidationError):
            ActOutcome.model_validate({"status": "done", "reply": "Okay.", "speaker": "ada"})


class TestWhatIsAsked:
    def test_a_request_is_a_message_and_whether_untrusted_words_are_in_the_turn(self) -> None:
        assert ActRequest(message="turn on the lamp").untrusted_content_in_turn is False
        assert ActRequest(message="x", untrusted_content_in_turn=True).untrusted_content_in_turn

    def test_a_message_is_as_long_as_a_turn_takes(self) -> None:
        ActRequest(message="x" * MAX_MESSAGE_CHARS)
        with pytest.raises(ValidationError):
            ActRequest(message="x" * (MAX_MESSAGE_CHARS + 1))
        with pytest.raises(ValidationError):
            ActRequest(message="   ")


class TestTheCatalog:
    def test_a_skill_is_installed_for_the_tenant_unless_it_is_a_persons_own(self) -> None:
        assert SkillInstall(source="---\nname: x\n---").owner is SkillOwner.TENANT
        assert SkillInstall(source="x", owner=SkillOwner.USER).owner is SkillOwner.USER

    def test_nothing_runs_until_a_version_is_enabled(self) -> None:
        assert info().enabled_version is None

    def test_a_version_to_enable_is_a_number_from_one_or_the_latest(self) -> None:
        assert SkillEnable().version is None
        assert SkillEnable(version=2).version == 2
        with pytest.raises(ValidationError):
            SkillEnable(version=0)

    def test_a_skill_in_detail_has_its_versions_and_what_was_granted(self) -> None:
        detail = SkillDetail(
            **info(latest_version=2, enabled_version=1).model_dump(),
            manifest=manifest(),
            instructions="Turn it on or off.",
            versions=(
                SkillVersionInfo(
                    version=1,
                    content_sha256="a" * 64,
                    created_at=NOW,
                    enabled=True,
                    enabled_at=NOW,
                ),
                SkillVersionInfo(version=2, content_sha256="b" * 64, created_at=NOW, enabled=False),
            ),
            granted=SkillPermissions(network=("api.example.org",)),
        )

        assert [v.enabled for v in detail.versions] == [True, False]
        assert detail.granted is not None
        assert detail.granted.network == ("api.example.org",)

    def test_a_version_is_known_by_the_hash_of_what_it_says(self) -> None:
        with pytest.raises(ValidationError):
            SkillVersionInfo(version=1, content_sha256="not-a-hash", created_at=NOW, enabled=False)

    def test_lists_are_empty_by_default(self) -> None:
        assert SkillList().skills == ()
        assert AuditList().records == ()
        assert SkillReload().problems == ()


class TestWhatIsRunWith:
    def test_a_run_is_given_the_parameters_the_device_parameters_and_what_was_granted(self) -> None:
        request = RunRequest(
            skill_id=SkillId(uuid4()),
            skill_name="lamp",
            version=3,
            mode=SkillMode.SCRIPT,
            entrypoint="scripts/run.py",
            params={"state": "on"},
            device_params={"entity_id": "light.living_room"},
            permissions=SkillPermissions(network=("homeassistant.local",)),
            timeout_s=10,
        )

        assert request.permissions.network == ("homeassistant.local",)
        assert request.params == {"state": "on"}

    @pytest.mark.parametrize("timeout_s", [0, 121])
    def test_a_run_has_a_time_limit_of_a_couple_of_minutes_at_most(self, timeout_s: int) -> None:
        with pytest.raises(ValidationError):
            RunRequest(
                skill_id=SkillId(uuid4()),
                skill_name="lamp",
                version=1,
                mode=SkillMode.SCRIPT,
                timeout_s=timeout_s,
            )

    def test_what_comes_out_is_a_message_data_and_commands_for_devices(self) -> None:
        result = RunResult(message="Done.", data={"state": "on"}, commands=(SetVolume(level=30),))

        assert result.commands == (SetVolume(level=30),)
        assert result.dry_run is False

    def test_a_script_cannot_order_a_flood_of_commands(self) -> None:
        RunResult(commands=tuple(SetVolume(level=1) for _ in range(MAX_COMMANDS_PER_RUN)))
        with pytest.raises(ValidationError):
            RunResult(commands=tuple(SetVolume(level=1) for _ in range(MAX_COMMANDS_PER_RUN + 1)))

    def test_a_command_that_is_not_in_the_vocabulary_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RunResult.model_validate({"commands": [{"name": "format_disk"}]})


class TestTheConversationLearnsToAct:
    def test_acting_is_a_phase_of_a_turn_and_the_actions_being_away_is_a_warning(self) -> None:
        assert Phase.ACTING.value == "acting"
        assert WarningCode.ACTIONS_UNAVAILABLE.value == "actions_unavailable"
