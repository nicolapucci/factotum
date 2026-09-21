"""The skills in ``skills/examples`` are what the documentation tells people to try: they must load,
and the file of devices that binds them must agree with what they declare."""

from pathlib import Path

import pytest

from factotum.actions.skills import LoadedSkill, load_folder
from factotum.contracts.effects import Effect
from factotum.contracts.skills import ApprovalPolicy, ConfirmPolicy, SkillMode, SkillTarget
from factotum.contracts.testing import make_tenant_id, make_user_id
from factotum.devices.home import parse_home

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "skills" / "examples"
HOME = ROOT / "config" / "devices.example.yaml"


def examples() -> dict[str, LoadedSkill]:
    skills, problems = load_folder(EXAMPLES)
    assert problems == []
    return {skill.manifest.name: skill for skill in skills}


def test_every_example_skill_is_a_skill() -> None:
    assert sorted(examples()) == [
        "garage-door",
        "lamp",
        "restart-router",
        "volume",
        "weather",
    ]


@pytest.mark.parametrize("name", sorted(examples()))
def test_each_example_runs_a_script_that_is_in_its_folder(name: str) -> None:
    skill = examples()[name]

    assert skill.manifest.mode is SkillMode.SCRIPT
    assert skill.manifest.entrypoint in skill.files


def test_the_examples_show_each_way_an_action_can_be_decided() -> None:
    by_name = examples()

    assert by_name["weather"].manifest.effect is Effect.READ_ONLY
    assert by_name["garage-door"].manifest.confirm is ConfirmPolicy.ASK
    assert by_name["restart-router"].manifest.approval is ApprovalPolicy.REQUIRED
    assert by_name["volume"].manifest.target is SkillTarget.SYSTEM
    assert by_name["lamp"].manifest.target is SkillTarget.EXTERNAL


def test_what_a_script_may_reach_is_what_its_skill_declares() -> None:
    weather = examples()["weather"]

    assert set(weather.manifest.permissions.network) == {
        "geocoding-api.open-meteo.com",
        "api.open-meteo.com",
    }
    for host in weather.manifest.permissions.network:
        assert host in weather.files["scripts/run.py"].decode()


class TestTheFileOfDevicesAgreesWithThem:
    def bindings(self) -> list[tuple[str, dict[str, object]]]:
        home = parse_home(
            HOME.read_text(encoding="utf-8"),
            tenant_id=make_tenant_id(),
            user_id=make_user_id(),
            source=HOME.name,
        )
        return [(binding.skill, dict(binding.params)) for binding in home.bindings]

    def test_every_binding_is_of_an_example_skill_that_acts_on_an_external_device(self) -> None:
        skills = examples()

        for name, _ in self.bindings():
            assert name in skills
            assert skills[name].manifest.target is SkillTarget.EXTERNAL

    def test_every_binding_gives_the_parameters_its_skill_declares_and_no_others(self) -> None:
        skills = examples()

        for name, params in self.bindings():
            declared = skills[name].manifest.device_parameters
            assert set(params) == set(declared), name
            assert all(
                isinstance(params[key], str) for key in declared if declared[key].type == "string"
            )
