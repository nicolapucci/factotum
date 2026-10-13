"""The corpus a model is measured against must be one that the service can meet: with a planner that
reads every message as it is meant, each case comes out as its own expectation says. What is then
left for the live test to measure is the model alone."""

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from factotum.actions.core import platform_skills
from factotum.actions.plan_cases import DISPLAY, KITCHEN, PHONE, PLAN_CASES, PlanCase
from factotum.actions.rig import ActionsRig, plan_of
from factotum.actions.skills import load_folder
from factotum.actions.testing import FakeSpeech
from factotum.actions.values import check_values
from factotum.contracts.actions import ActStatus
from factotum.contracts.commands import CommandResult
from factotum.contracts.skills import SkillManifest, SkillTarget

ROOT = Path(__file__).resolve().parents[3]
HOME_TEXT = (ROOT / "config" / "devices.example.yaml").read_text("utf-8")
EXAMPLES = ROOT / "skills" / "examples"

ITALIAN = (
    "accendi", "spegni", "metti", "apri", "chiudi", "alza", "abbassa", "che ", "riavvia",
    "com'è", "annuncia", "dì ", "prepara",
)  # fmt: skip


def manifests() -> dict[str, SkillManifest]:
    """The skills of the example home: those of the folder, and those that the platform provides."""
    skills, problems = load_folder(EXAMPLES)
    assert problems == []
    found = {skill.manifest.name: skill.manifest for skill in skills}
    return {**found, **platform_skills(FakeSpeech()).manifests()}


def ideal_plan(case: PlanCase) -> dict[str, Any]:
    """What a model that reads the message as it is meant says."""
    if case.skill is None:
        return plan_of(None, reason="a question, not a request")
    asks = case.status is ActStatus.NEEDS_CONFIRMATION
    return plan_of(
        case.skill,
        device=case.device,
        parameters=case.params,
        question="Do you really want me to do that?" if asks else None,
    )


class TestTheCorpus:
    def test_it_is_not_a_handful_of_messages(self) -> None:
        assert len(PLAN_CASES) >= 45

    def test_no_message_is_there_twice_for_the_same_device(self) -> None:
        seen = Counter((case.message, case.speaker) for case in PLAN_CASES)

        assert [key for key, count in seen.items() if count > 1] == []

    def test_a_good_part_of_what_is_said_is_a_question_and_most_of_it_is_not(self) -> None:
        questions = sum(1 for case in PLAN_CASES if case.skill is None)

        assert 0.25 <= questions / len(PLAN_CASES) <= 0.5

    def test_every_skill_is_asked_for_in_english_and_in_italian(self) -> None:
        for name in manifests():
            asked = [case.message for case in PLAN_CASES if case.skill == name]
            assert len(asked) >= 3, name
            assert any(m.startswith(ITALIAN) for m in asked), f"{name} is not asked for in Italian"
            assert not all(m.startswith(ITALIAN) for m in asked), f"{name} is only asked in Italian"

    def test_a_question_asks_for_no_skill_and_a_command_asks_for_one_that_is_there(self) -> None:
        skills = manifests()

        for case in PLAN_CASES:
            assert (case.skill is None) == (case.status is ActStatus.NO_SKILL), case.message
            assert case.skill is None or case.skill in skills, case.message

    def test_a_speaker_is_a_device_of_the_example_home(self) -> None:
        known = {device.name for device, _ in ActionsRig(home_text=HOME_TEXT).home.system}

        assert {case.speaker for case in PLAN_CASES} <= known
        assert {KITCHEN, DISPLAY, PHONE} <= known

    def test_the_values_of_a_command_are_ones_its_skill_declares_accepts_and_the_message_says(
        self,
    ) -> None:
        skills = manifests()

        for case in PLAN_CASES:
            if case.skill is None:
                continue
            checked = check_values(skills[case.skill], case.params, said=case.message)
            assert checked.invalid == (), case.message
            assert checked.unsaid == (), case.message
            assert checked.values == case.params, case.message

    def test_a_device_is_one_that_the_skill_acts_on(self) -> None:
        """An external device that the skill is bound to, or a system one for a skill that acts on
        the device that was named."""
        home = ActionsRig(home_text=HOME_TEXT).home
        skills = manifests()
        bound = {(binding.skill, binding.device_name) for binding in home.bindings}
        external = {device.name for device in home.external}
        system = {device.name for device, _ in home.system}

        for case in PLAN_CASES:
            if case.device is None:
                continue
            assert case.skill is not None
            target = skills[case.skill].target
            if target is SkillTarget.SYSTEM:
                assert case.device in system, case.message
                continue
            assert target is SkillTarget.EXTERNAL, case.message
            assert (case.skill, case.device) in bound, case.message
            assert case.device in external, case.message


def case_id(index: int, case: PlanCase) -> str:
    return f"{index:02d}-{case.speaker.split()[0].lower()}"


@pytest.mark.parametrize(
    "case", PLAN_CASES, ids=[case_id(i, case) for i, case in enumerate(PLAN_CASES)]
)
async def test_a_planner_that_reads_it_right_gets_the_outcome_the_case_expects(
    case: PlanCase,
) -> None:
    # What is looked at is what the code makes of a plan, and not what the stand-in of the embedding
    # model thinks is near a message: every skill that is enabled is offered to the model.
    rig = ActionsRig(
        home_text=HOME_TEXT, core=platform_skills(FakeSpeech()), every_skill_is_offered=True
    )
    rig.dispatcher.answer = CommandResult.ok(
        {"volume": 50, "muted": False}
    )  # a device says how it is
    skills, _ = load_folder(EXAMPLES)
    for skill in skills:  # enabled but not indexed
        installed = await rig.store.install(rig.tenant, rig.shared_owner, skill, now=rig.clock())
        await rig.store.enable(
            rig.tenant,
            installed.skill_id,
            installed.version,
            granted=skill.manifest.permissions,
            by=rig.user,
            now=rig.clock(),
        )
    rig.plans(ideal_plan(case))

    outcome = await rig.hear(case.speaker, case.message)

    assert outcome.status is case.status, (case.message, outcome.reply)
    assert outcome.skill == case.skill
