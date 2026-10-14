"""The corpus for the mock skills must be one that the service can meet: with a planner that reads
every message as it is meant, each case comes out as its expectation says, and what a skill answers
is what its mock says, in the language of the message. What is then left for the live test to
measure is the model alone, with skills that it was not shown anything like."""

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from factotum.actions.core import platform_skills
from factotum.actions.measure import measure
from factotum.actions.mock_cases import MOCK_CASES
from factotum.actions.mock_runner import MOCK_FILE, MockRunner, read_mock
from factotum.actions.plan_cases import DISPLAY, KITCHEN, PHONE, PlanCase
from factotum.actions.rig import ActionsRig, plan_of
from factotum.actions.skills import LoadedSkill, load_folder
from factotum.actions.testing import FakeSpeech, a_device_answers
from factotum.actions.values import check_values
from factotum.common.language import detect_language
from factotum.contracts.actions import ActStatus
from factotum.contracts.formats import FormatKind
from factotum.contracts.skills import SkillManifest

ROOT = Path(__file__).resolve().parents[3]
HOME_TEXT = (ROOT / "config" / "devices.example.yaml").read_text("utf-8")
SKILLS = ROOT / "skills"
MOCKS = SKILLS / "mocks"


def skills() -> dict[str, LoadedSkill]:
    loaded, problems = load_folder(SKILLS)
    assert problems == []
    return {skill.manifest.name: skill for skill in loaded}


def mocks() -> dict[str, SkillManifest]:
    loaded, _ = load_folder(MOCKS)
    return {skill.manifest.name: skill.manifest for skill in loaded}


def language_of(case: PlanCase) -> str:
    return "it" if detect_language(case.message) == "it" else "en"


def written(case: PlanCase) -> dict[str, Any]:
    """The values as a model that reads the message well writes them: the words that were said for
    a value that a skill takes in a form (ADR 0030), and the value itself for the others."""
    return {**case.params, **case.words}


def ideal_plan(case: PlanCase) -> dict[str, Any]:
    """What a model that reads the message as it is meant says."""
    if case.skill is None:
        return plan_of(None, reason="a question, not a request")
    asks = case.status is ActStatus.NEEDS_CONFIRMATION
    return plan_of(
        case.skill,
        device=case.device,
        parameters=written(case),
        language=language_of(case),
        question="Do you really want me to do that?" if asks else None,
    )


class TestTheCorpus:
    def test_it_is_a_good_number_of_messages_and_questions_are_among_them(self) -> None:
        questions = sum(1 for case in MOCK_CASES if case.skill is None)

        assert len(MOCK_CASES) >= 150
        assert 0.12 <= questions / len(MOCK_CASES) <= 0.35

    def test_no_message_is_there_twice_for_the_same_device(self) -> None:
        seen = Counter((case.message, case.speaker) for case in MOCK_CASES)

        assert [key for key, count in seen.items() if count > 1] == []

    def test_every_mock_is_asked_for_in_english_and_in_italian_and_more_than_a_few_times(
        self,
    ) -> None:
        for name in mocks():
            asked = [case for case in MOCK_CASES if case.skill == name]
            assert len(asked) >= 4, name
            assert {language_of(case) for case in asked} == {"it", "en"}, name

    def test_every_mock_is_asked_for_what_it_needs_and_without_it(self) -> None:
        for name, manifest in mocks().items():
            if not manifest.required:
                continue
            asked = [case for case in MOCK_CASES if case.skill == name]
            assert any(case.status is ActStatus.NEEDS_MORE for case in asked), name
            assert any(case.status is ActStatus.DONE or case.params for case in asked), name

    def test_a_message_that_is_for_no_skill_is_for_none_and_the_others_are_for_a_mock(self) -> None:
        for case in MOCK_CASES:
            assert (case.skill is None) == (case.status is ActStatus.NO_SKILL), case.message
            assert case.skill is None or case.skill in mocks(), case.message

    def test_a_speaker_is_a_device_of_the_example_home(self) -> None:
        known = {device.name for device, _ in ActionsRig(home_text=HOME_TEXT).home.system}

        assert {case.speaker for case in MOCK_CASES} <= known
        assert {KITCHEN, DISPLAY, PHONE} <= known

    def test_what_a_case_wants_the_code_to_make_of_the_values_is_what_it_makes(self) -> None:
        """The values are ones the message says, and those that are to be asked for again are, in
        the code, asked for again: so the corpus is not wrong about the checks."""
        manifests = mocks()
        for case in MOCK_CASES:
            if case.skill is None:
                continue
            checked = check_values(manifests[case.skill], written(case), said=case.message)
            assert checked.unsaid == (), case.message
            if case.status is ActStatus.NEEDS_MORE:
                assert checked.asked_for, case.message
            else:
                assert checked.invalid == (), case.message
                assert checked.missing == (), case.message
                assert checked.values == case.params, case.message

    def test_a_value_that_is_taken_in_a_form_has_the_words_a_model_writes_for_it(self) -> None:
        """What is said is not what the skill is given (ADR 0030): a case that is done says both."""
        for case in MOCK_CASES:
            if case.skill is None or case.status is not ActStatus.DONE:
                continue
            formatted = {
                name for name, spec in mocks()[case.skill].parameters.items() if spec.formats
            }
            assert set(case.words) == formatted & set(case.params), case.message
            assert set(case.words) <= set(case.params), case.message

    def test_every_kind_of_format_is_asked_for_and_given_at_least_once(self) -> None:
        kinds = {
            fmt.kind
            for case in MOCK_CASES
            if case.skill is not None and case.status is ActStatus.DONE
            for name in case.words
            for fmt in mocks()[case.skill].parameters[name].formats or ()
        }

        assert kinds == set(FormatKind)

    def test_what_a_form_cannot_say_is_asked_for_and_not_guessed(self) -> None:
        """A day with no year for a skill that wants a date, and *tomorrow* for one that does not
        count days (the platform has no clock), are not values."""
        asked = {case.message for case in MOCK_CASES if case.status is ActStatus.NEEDS_MORE}

        assert "biglietto del treno per Napoli domani" in asked
        assert "book a train ticket to Florence on October 16" in asked

    def test_what_is_irreversible_is_refused_to_the_shared_speaker_and_asked_about_elsewhere(
        self,
    ) -> None:
        for case in MOCK_CASES:
            if (
                case.skill not in {"door-lock", "send-message"}
                or case.status is ActStatus.NEEDS_MORE
            ):
                continue
            assert (case.status is ActStatus.REFUSED) == (case.speaker == KITCHEN), case.message


async def a_rig() -> ActionsRig:
    """The service with the skills of the platform, the examples and the mocks, all enabled and
    offered, and the mock runner for the ones that run."""
    rig = ActionsRig(
        home_text=HOME_TEXT, core=platform_skills(FakeSpeech()), every_skill_is_offered=True
    )
    rig.dispatcher.responder = a_device_answers
    rig.use_runner(MockRunner(rig.store))
    for skill in skills().values():
        installed = await rig.store.install(rig.tenant, rig.shared_owner, skill, now=rig.clock())
        await rig.store.enable(
            rig.tenant,
            installed.skill_id,
            installed.version,
            granted=skill.manifest.permissions,
            by=rig.user,
            now=rig.clock(),
        )
        stored = await rig.store.get_skill(rig.tenant, installed.skill_id)
        assert stored is not None
        assert await rig.index.index(stored)  # the ones that are not indexed are offered unscored
    return rig


def case_id(index: int, case: PlanCase) -> str:
    return f"{index:03d}-{case.speaker.split()[0].lower()}"


@pytest.mark.parametrize(
    "case", MOCK_CASES, ids=[case_id(i, case) for i, case in enumerate(MOCK_CASES)]
)
async def test_a_planner_that_reads_it_right_gets_the_outcome_the_case_expects(
    case: PlanCase,
) -> None:
    rig = await a_rig()
    rig.plans(ideal_plan(case))

    judged = await measure(rig, case)

    assert judged.outcome.status is case.status, (case.message, judged.outcome.reply)
    assert judged.outcome.skill == case.skill
    assert judged.problems == [], case.message


@pytest.mark.parametrize(
    "case",
    [case for case in MOCK_CASES if case.status is ActStatus.DONE],
    ids=lambda case: case.message,
)
async def test_a_skill_that_is_done_answers_as_its_mock_says_in_the_language_of_the_message(
    case: PlanCase,
) -> None:
    assert case.skill is not None
    rig = await a_rig()
    rig.plans(ideal_plan(case))
    mock = read_mock(skills()[case.skill].files[MOCK_FILE])
    wanted = mock.answer(case.params).says(language_of(case), case.params)

    outcome = await rig.hear(case.speaker, case.message)

    assert outcome.reply == wanted
    assert "Dry run" not in (outcome.reply or "")
    assert wanted.strip()


async def test_a_mock_that_is_asked_about_first_is_not_answered_until_the_yes() -> None:
    rig = await a_rig()
    case = next(
        c for c in MOCK_CASES if c.skill == "door-lock" and c.status is ActStatus.NEEDS_CONFIRMATION
    )
    rig.plans(ideal_plan(case))

    asked = await rig.hear(case.speaker, case.message)

    assert asked.status is ActStatus.NEEDS_CONFIRMATION
    [record] = await rig.audit()
    assert record.params == case.params
    assert record.result.value == "not_executed"
