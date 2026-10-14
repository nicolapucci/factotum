"""The mock skills of ``skills/mocks`` and the ``mock.yaml`` of the examples: skills that do nothing
and answer as they would, so that how the assistant chooses a skill and calls it can be measured and
listened to without anything being done. What is held here is that each mock is a skill the system
can use as its manifest says, and that what it answers is something plausible for every call the
manifest allows."""

import itertools
from pathlib import Path
from typing import Any

import pytest

from factotum.actions.mock_runner import MOCK_FILE, MockAnswers, blanks_of, read_mock
from factotum.actions.skills import LoadedSkill, load_folder
from factotum.contracts.formats import FormatKind
from factotum.contracts.skills import ParameterSpec, ParamType, ParamValue, SkillTarget

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "skills"
MOCKS = SKILLS / "mocks"
EXAMPLES = SKILLS / "examples"


def skills_of(folder: Path) -> dict[str, LoadedSkill]:
    loaded, problems = load_folder(folder)
    assert problems == []
    return {skill.manifest.name: skill for skill in loaded}


ALL = {**skills_of(EXAMPLES), **skills_of(MOCKS)}


def mock_of(skill: LoadedSkill) -> MockAnswers:
    return read_mock(skill.files[MOCK_FILE])


def samples(name: str, spec: ParameterSpec) -> list[ParamValue]:
    """Values a call may have for a parameter: every choice of a list, and one value otherwise."""
    if spec.enum is not None:
        return list(spec.enum)
    match spec.type:
        case ParamType.STRING:
            return ["something"]
        case ParamType.BOOLEAN:
            return [True]
        case ParamType.INTEGER:
            return [int(spec.minimum or 1)]
        case ParamType.NUMBER:
            return [float(spec.minimum or 1)]


def calls_of(skill: LoadedSkill) -> list[dict[str, ParamValue]]:
    """Every call that the manifest allows: each required value, each optional one given or not,
    and every choice of a list."""
    manifest = skill.manifest
    options: list[list[Any]] = []
    names = list(manifest.parameters)
    for name in names:
        values = samples(name, manifest.parameters[name])
        options.append(values if name in manifest.required else [None, *values])
    calls = [
        {name: value for name, value in zip(names, combination, strict=True) if value is not None}
        for combination in itertools.product(*options)
    ]
    assert len(calls) <= 200, skill.manifest.name
    return calls


def test_the_examples_and_the_mocks_are_all_skills_that_say_how_they_answer() -> None:
    assert len(ALL) >= 21
    assert [name for name, skill in ALL.items() if MOCK_FILE not in skill.files] == []


@pytest.mark.parametrize("name", sorted(ALL))
class TestEachMock:
    def test_it_is_a_mock_file(self, name: str) -> None:
        assert mock_of(ALL[name]).replies

    def test_what_a_reply_wants_to_see_is_a_value_the_skill_has(self, name: str) -> None:
        skill = ALL[name]
        for reply in mock_of(skill).replies:
            for key, wanted in reply.when.items():
                spec = skill.manifest.parameters.get(key)
                assert spec is not None, f"{name}: when {key} is not a parameter"
                if wanted != "*" and spec.enum is not None:
                    assert wanted in spec.enum, f"{name}: when {key} is {wanted!r}"

    def test_a_blank_is_a_value_the_skill_has_and_one_that_is_given_when_it_is_said(
        self, name: str
    ) -> None:
        skill = ALL[name]
        required = set(skill.manifest.required)
        for reply in mock_of(skill).replies:
            for blank in reply.blanks:
                assert blank in skill.manifest.parameters, f"{name}: {blank} is not a parameter"
                assert blank in required or blank in reply.when, (
                    f"{name}: {blank} may not have been given when this reply is said"
                )

    def test_every_call_that_the_manifest_allows_has_something_to_say_in_both_languages(
        self, name: str
    ) -> None:
        skill = ALL[name]
        mock = mock_of(skill)
        for call in calls_of(skill):
            reply = mock.answer(call)
            for language in ("it", "en"):
                said = reply.says(language, call)
                assert said.strip(), f"{name} {call} is not answered in {language}"
                assert "  " not in said, f"{name} {call}: a blank was left empty in {said!r}"

    def test_the_two_languages_use_the_same_blanks(self, name: str) -> None:
        for reply in mock_of(ALL[name]).replies:
            assert blanks_of(reply.it) == blanks_of(reply.en)


@pytest.mark.parametrize("name", sorted(skills_of(MOCKS)))
class TestEachMockSkill:
    def test_it_is_a_skill_with_nothing_to_run_and_nothing_to_reach(self, name: str) -> None:
        manifest = ALL[name].manifest

        assert manifest.entrypoint is None
        assert manifest.permissions.network == ()
        assert manifest.permissions.secrets == ()
        assert manifest.permissions.accounts == ()

    def test_it_needs_no_device_that_the_home_would_have_to_have(self, name: str) -> None:
        assert ALL[name].manifest.target in {SkillTarget.NONE, SkillTarget.SYSTEM}

    def test_it_is_found_by_what_it_says_of_itself_in_both_languages(self, name: str) -> None:
        manifest = ALL[name].manifest

        assert len(manifest.examples) >= 3
        assert len(manifest.description) < 200  # said as a person who writes a skill says it

    def test_it_is_in_the_folder_of_its_name(self, name: str) -> None:
        assert (MOCKS / name / "SKILL.md").is_file()


def test_the_mocks_are_in_one_folder_and_are_not_the_examples() -> None:
    assert set(skills_of(MOCKS)).isdisjoint(skills_of(EXAMPLES))
    assert len(skills_of(MOCKS)) >= 15


def test_the_mocks_cover_the_shapes_that_a_manifest_can_have() -> None:
    manifests = [skill.manifest for name, skill in ALL.items() if name in skills_of(MOCKS)]
    parameters = [spec for manifest in manifests for spec in manifest.parameters.values()]

    assert {spec.type for spec in parameters} == set(ParamType)
    assert any(spec.enum for spec in parameters)
    assert any(spec.minimum is not None and spec.maximum is not None for spec in parameters)
    assert any(manifest.required == () for manifest in manifests)  # all optional
    assert any(set(manifest.parameters) - set(manifest.required) for manifest in manifests)
    assert any(manifest.confirm.value == "ask" for manifest in manifests)
    assert {manifest.effect.value for manifest in manifests} == {
        "read_only",
        "reversible",
        "irreversible",
    }
    assert {manifest.target for manifest in manifests} == {SkillTarget.NONE, SkillTarget.SYSTEM}
    assert {fmt.kind for spec in parameters for fmt in spec.formats or ()} == set(FormatKind)
    assert {fmt.pattern for spec in parameters for fmt in spec.formats or ()} >= {
        "seconds",
        "minutes",
        "iso",
        "name",
    }
