"""The skills in ``skills/examples`` are what the documentation tells people to try: they must load,
and the file of devices that binds them must agree with what they declare."""

import io
import json
import re
import runpy
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from factotum.actions.core import platform_skills
from factotum.actions.instructions.checks import check_plan
from factotum.actions.instructions.executor import Context, run
from factotum.actions.instructions.testing import FakeWeb, plan_dict, step_http
from factotum.actions.planner import EXAMPLES as PLANNER_EXAMPLES
from factotum.actions.planner import view_of
from factotum.actions.runner_client import hosts_of, read_output
from factotum.actions.skills import LoadedSkill, load_folder
from factotum.actions.testing import FakeSpeech
from factotum.common.hosts import match_host
from factotum.contracts.commands import AdjustVolume, SetVolume, VolumeDirection
from factotum.contracts.effects import Effect
from factotum.contracts.errors import SkillFailedError
from factotum.contracts.instructions import InstructionPlan
from factotum.contracts.runner import JobResult, RunResult
from factotum.contracts.skills import (
    MAX_INSTRUCTIONS_CHARS,
    ApprovalPolicy,
    ConfirmPolicy,
    SkillMode,
    SkillTarget,
)
from factotum.contracts.testing import make_tenant_id, make_user_id
from factotum.devices.home import parse_home

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "skills" / "examples"
HOME = ROOT / "config" / "devices.example.yaml"


def examples() -> dict[str, LoadedSkill]:
    skills, problems = load_folder(EXAMPLES)
    assert problems == []
    return {skill.manifest.name: skill for skill in skills}


def test_the_skills_that_the_planner_is_shown_as_examples_are_shown_as_these_are() -> None:
    """What the model has been shown how to read is what it is given for a real skill: the same
    values, with the same words about what each is for."""
    manifests = {name: skill.manifest for name, skill in examples().items()}
    manifests |= platform_skills(FakeSpeech()).manifests()  # announce is the platform's
    shown = {view.name: view for example in PLANNER_EXAMPLES for view in example.skills}

    assert shown
    for name, view in shown.items():
        real = view_of(manifests[name])
        assert (view.description, view.target, view.values) == (
            real.description,
            real.target,
            real.values,
        ), name


def test_every_example_skill_is_a_skill() -> None:
    # announce is not here: it is a skill of the platform (``factotum.actions.core``)
    assert sorted(examples()) == [
        "air-quality",
        "garage-door",
        "lamp",
        "movie-scene",
        "restart-router",
        "volume",
        "volume-change",
        "weather",
    ]


def scripts() -> list[str]:
    return sorted(
        name for name, skill in examples().items() if skill.manifest.mode is SkillMode.SCRIPT
    )


@pytest.mark.parametrize("name", scripts())
def test_each_example_that_has_a_script_runs_one_that_is_in_its_folder(name: str) -> None:
    skill = examples()[name]

    assert skill.manifest.mode is SkillMode.SCRIPT
    assert skill.manifest.entrypoint in skill.files


def test_the_examples_show_each_way_an_action_can_be_decided() -> None:
    by_name = examples()

    assert by_name["weather"].manifest.effect is Effect.READ_ONLY
    assert by_name["garage-door"].manifest.confirm is ConfirmPolicy.ASK
    assert by_name["restart-router"].manifest.approval is ApprovalPolicy.REQUIRED
    assert by_name["volume"].manifest.target is SkillTarget.SYSTEM
    assert by_name["volume-change"].manifest.target is SkillTarget.SYSTEM
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


class FakeResponse:
    """What ``urlopen`` gives back: something that is read, and that may be used as a context."""

    def __init__(self, body: object) -> None:
        self._body = io.BytesIO(json.dumps(body).encode())

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exception: object) -> None:
        return None


class TestTheScriptsDoWhatTheirSkillsSay:
    """Each script is run as it is (its input on standard input, its secrets in the environment,
    the network replaced by a double that records what would have been asked), and what it prints
    is read the way the runner's answer is read: the scripts and the protocol agree, and a script
    reaches only what its skill declares."""

    def run(
        self,
        name: str,
        stdin: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        *,
        env: dict[str, str] | None = None,
        answers: list[object] | None = None,
    ) -> tuple[list[Any], RunResult]:
        script = EXAMPLES / name / "scripts" / "run.py"
        calls: list[Any] = []
        replies = list(answers or [{}])

        def urlopen(target: Any, timeout: float | None = None) -> FakeResponse:
            calls.append(target)
            return FakeResponse(replies.pop(0))

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin)))
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        code: object = 0
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as stopped:
            code = stopped.code
        assert code in {0, None}
        printed = capsys.readouterr().out
        try:
            return calls, read_output(name, JobResult(exit_code=0, stdout=printed))
        except SkillFailedError as failed:  # a script that says why it could not do it
            return calls, RunResult(error=failed.message)

    @staticmethod
    def reaches(name: str, device: dict[str, Any], calls: list[Any]) -> set[str | None]:
        """The hosts that were asked for, and that must be some of those the skill declares."""
        declared = set(hosts_of(examples()[name].manifest.permissions, device))
        asked = {
            urllib.parse.urlsplit(call if isinstance(call, str) else call.full_url).hostname
            for call in calls
        }
        assert asked <= declared, (asked, declared)
        return asked

    @pytest.mark.parametrize(
        ("params", "service", "body", "message"),
        [
            (
                {"state": "on", "brightness": 30},
                "turn_on",
                {"entity_id": "light.living_room", "brightness_pct": 30},
                "The lamp is on.",
            ),
            ({"state": "on"}, "turn_on", {"entity_id": "light.living_room"}, "The lamp is on."),
            ({"state": "off"}, "turn_off", {"entity_id": "light.living_room"}, "The lamp is off."),
            (
                {"state": "off", "brightness": 30},
                "turn_off",
                {"entity_id": "light.living_room"},
                "The lamp is off.",
            ),
        ],
        ids=["dimmed", "on", "off", "off-ignores-brightness"],
    )
    def test_the_lamp_is_switched_through_home_assistant(
        self,
        params: dict[str, object],
        service: str,
        body: dict[str, object],
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        device = {"entity_id": "light.living_room", "host": "ha.example"}

        calls, result = self.run(
            "lamp",
            {"params": params, "device_params": device},
            monkeypatch,
            capsys,
            env={"HA_TOKEN": "token-1"},
        )

        (call,) = calls
        assert call.full_url == f"http://ha.example:8123/api/services/light/{service}"
        assert call.get_header("Authorization") == "Bearer token-1"
        assert json.loads(call.data) == body
        assert result.message == message
        assert self.reaches("lamp", device, calls) == {"ha.example"}

    @pytest.mark.parametrize(
        ("action", "service", "message"),
        [
            ("open", "open_cover", "The garage door is opening."),
            ("close", "close_cover", "The garage door is closing."),
        ],
    )
    def test_the_garage_door_is_moved_through_home_assistant(
        self,
        action: str,
        service: str,
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        device = {"entity_id": "cover.garage", "host": "192.168.1.20"}

        calls, result = self.run(
            "garage-door",
            {"params": {"action": action}, "device_params": device},
            monkeypatch,
            capsys,
            env={"HA_TOKEN": "token-2"},
        )

        (call,) = calls
        assert call.full_url == f"http://192.168.1.20:8123/api/services/cover/{service}"
        assert call.get_header("Authorization") == "Bearer token-2"
        assert json.loads(call.data) == {"entity_id": "cover.garage"}
        assert result.message == message
        assert self.reaches("garage-door", device, calls) == {"192.168.1.20"}

    def test_the_volume_is_a_command_for_the_device_and_reaches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls, result = self.run(
            "volume", {"params": {"level": 35}, "device_params": {}}, monkeypatch, capsys
        )

        assert calls == []
        assert result.message == "Volume set to 35."
        assert result.commands == (SetVolume(level=35),)

    @pytest.mark.parametrize(
        ("params", "command", "message"),
        [
            (
                {"direction": "up"},
                AdjustVolume(direction=VolumeDirection.UP),
                "Volume up.",
            ),
            (
                {"direction": "down"},
                AdjustVolume(direction=VolumeDirection.DOWN),
                "Volume down.",
            ),
            (
                {"direction": "up", "amount": 25},
                AdjustVolume(direction=VolumeDirection.UP, amount=25),
                "Volume up by 25.",
            ),
            (
                {"direction": "down", "amount": 5},
                AdjustVolume(direction=VolumeDirection.DOWN, amount=5),
                "Volume down by 5.",
            ),
        ],
    )
    def test_the_volume_is_turned_by_what_is_said_and_the_step_of_the_device_is_not_the_skills(
        self,
        params: dict[str, object],
        command: AdjustVolume,
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls, result = self.run(
            "volume-change", {"params": params, "device_params": {}}, monkeypatch, capsys
        )

        assert calls == []
        assert result.message == message
        assert result.commands == (command,)

    def test_only_the_direction_of_the_volume_is_asked_for_and_how_much_is_left_to_the_skill(
        self,
    ) -> None:
        manifest = examples()["volume-change"].manifest

        assert manifest.required == ("direction",)
        assert set(manifest.parameters) == {"direction", "amount"}

    def test_the_weather_of_a_city_is_looked_up_and_read(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        answers = [
            {"results": [{"name": "Rome", "latitude": 41.9, "longitude": 12.5}]},
            {"current": {"temperature_2m": 21.4, "precipitation": 0.0}},
        ]

        calls, result = self.run(
            "weather",
            {"params": {"place": "Rome"}, "device_params": {}},
            monkeypatch,
            capsys,
            answers=answers,
        )

        assert calls[0].endswith("/v1/search?count=1&name=Rome")
        assert "latitude=41.9&longitude=12.5" in calls[1]
        assert result.message == "In Rome it is 21 degrees."
        assert result.data == {"temperature_2m": 21.4, "precipitation": 0.0}
        assert self.reaches("weather", {}, calls) == {
            "geocoding-api.open-meteo.com",
            "api.open-meteo.com",
        }

    def test_rain_is_said(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        answers = [
            {"results": [{"name": "Milan", "latitude": 45.4, "longitude": 9.2}]},
            {"current": {"temperature_2m": 12.6, "precipitation": 1.2}},
        ]

        _, result = self.run(
            "weather",
            {"params": {"place": "Milan"}, "device_params": {}},
            monkeypatch,
            capsys,
            answers=answers,
        )

        assert result.message == "In Milan it is 13 degrees and it is raining."

    def test_a_place_is_quoted_before_it_is_put_in_a_url(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls, _ = self.run(
            "weather",
            {"params": {"place": "São Paulo & co"}, "device_params": {}},
            monkeypatch,
            capsys,
            answers=[{}],
        )

        assert calls[0].endswith("&name=S%C3%A3o%20Paulo%20%26%20co")

    def test_a_city_that_is_not_found_is_a_skill_that_says_why_it_could_not_do_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, result = self.run(
            "weather",
            {"params": {"place": "Atlantis"}, "device_params": {}},
            monkeypatch,
            capsys,
            answers=[{}],
        )

        assert result.error == "I could not find Atlantis."
        assert (result.message, result.commands) == (None, ())

    def test_the_router_is_asked_to_restart_with_its_password(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls, result = self.run(
            "restart-router",
            {"params": {}, "device_params": {}},
            monkeypatch,
            capsys,
            env={"ROUTER_PASSWORD": "pw-3"},
        )

        (call,) = calls
        assert (call.full_url, call.get_method()) == ("http://router.local/api/reboot", "POST")
        assert call.get_header("Authorization") == "Bearer pw-3"
        assert result.message == "The router is restarting."
        assert self.reaches("restart-router", {}, calls) == {"router.local"}

    @pytest.mark.parametrize("name", ["lamp", "garage-door", "restart-router"])
    def test_a_script_that_needs_a_secret_fails_when_it_is_not_given_one(
        self,
        name: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        device = {"entity_id": "x.y", "host": "ha.example"}
        for secret in examples()[name].manifest.permissions.secrets:
            monkeypatch.delenv(secret, raising=False)

        with pytest.raises(KeyError):
            self.run(
                name,
                {"params": {"state": "on", "action": "open"}, "device_params": device},
                monkeypatch,
                capsys,
            )

    def test_every_script_asks_for_no_secret_that_its_skill_does_not_declare(self) -> None:
        for name in scripts():
            skill = examples()[name]
            source = skill.files["scripts/run.py"].decode()
            asked = set(re.findall(r"os\.environ\[['\"]([A-Z_]+)['\"]\]", source))
            assert asked == set(skill.manifest.permissions.secrets), name


AUTHORIZATION = {"Authorization": "Bearer ${secret.HA_TOKEN}"}
LIGHTS = "http://homeassistant.local:8123/api/services/light"
GEOCODING = "https://geocoding-api.open-meteo.com/v1/search?count=1&name=${value.place}"
AIR = (
    "https://air-quality-api.open-meteo.com/v1/air-quality?current=european_aqi"
    "&latitude=${step.place.json.results.0.latitude}"
    "&longitude=${step.place.json.results.0.longitude}"
)

PLANS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    # what a model is to write for each of them, and what the person asked for
    "movie-scene": (
        plan_dict(
            step_http(
                "ceiling",
                "POST",
                f"{LIGHTS}/turn_off",
                headers=AUTHORIZATION,
                body={"entity_id": "light.living_room_ceiling"},
            ),
            step_http(
                "backlight",
                "POST",
                f"{LIGHTS}/turn_on",
                headers=AUTHORIZATION,
                body={"entity_id": "light.tv_backlight", "brightness_pct": 20},
            ),
            say="The movie scene is set.",
        ),
        {},
    ),
    "air-quality": (
        plan_dict(
            step_http("place", "GET", GEOCODING),
            step_http("air", "GET", AIR),
            say="The air quality index in ${value.place} is ${step.air.json.current.european_aqi}.",
        ),
        {"place": "Rome"},
    ),
}


class TestTheSkillsThatAreInstructions:
    """A skill that has no script is followed by a model, which writes a plan that is checked and
    run. These are the plans it is to write: the checks accept them for what each skill declares,
    and the program that runs them does what the notes say, against the shape of what the services
    answer."""

    @pytest.mark.parametrize("name", sorted(PLANS))
    def test_it_is_instructions_that_a_model_can_follow_and_has_no_files(self, name: str) -> None:
        skill = examples()[name]

        assert skill.manifest.mode is SkillMode.INSTRUCTIONS
        assert skill.manifest.entrypoint is None
        assert skill.files == {}
        assert 100 < len(skill.instructions) <= MAX_INSTRUCTIONS_CHARS

    def test_the_examples_that_are_instructions_are_the_ones_that_have_a_plan_here(self) -> None:
        by_mode = {
            name
            for name, skill in examples().items()
            if skill.manifest.mode is SkillMode.INSTRUCTIONS
        }

        assert by_mode == set(PLANS)

    @pytest.mark.parametrize("name", sorted(PLANS))
    def test_the_checks_accept_the_plan_for_what_the_skill_declares(self, name: str) -> None:
        skill = examples()[name]
        plan, values = PLANS[name]

        check_plan(
            InstructionPlan.model_validate(plan),
            manifest=skill.manifest,
            granted=skill.manifest.permissions,
            hosts=hosts_of(skill.manifest.permissions, {}),
            values=values,
            device_params={},
        )

    @pytest.mark.parametrize("name", ["movie-scene", "air-quality"])
    def test_what_the_notes_say_to_reach_is_what_the_skill_is_granted(self, name: str) -> None:
        skill = examples()[name]
        granted = hosts_of(skill.manifest.permissions, {})

        named = set(re.findall(r"https?://([a-z0-9.-]+)", skill.instructions))

        assert named
        assert all(match_host(granted, host) is not None for host in named), (named, granted)
        assert all(any(host == mentioned for mentioned in named) for host in granted)

    def test_a_skill_that_only_reads_is_declared_so_and_its_plan_only_reads(self) -> None:
        skill = examples()["air-quality"]
        plan, _ = PLANS["air-quality"]

        assert skill.manifest.effect is Effect.READ_ONLY
        assert {step["method"] for step in plan["steps"]} == {"GET"}

    def test_the_lights_are_a_reversible_change_that_needs_the_token(self) -> None:
        skill = examples()["movie-scene"]

        assert skill.manifest.effect is Effect.REVERSIBLE
        assert skill.manifest.permissions.secrets == ("HA_TOKEN",)

    def test_the_movie_scene_makes_two_calls_with_the_token_and_says_it_is_set(self) -> None:
        skill = examples()["movie-scene"]
        plan, values = PLANS["movie-scene"]
        with FakeWeb() as web:
            web.answer("POST", "/api/services/light/turn_off", json_body=[])
            web.answer("POST", "/api/services/light/turn_on", json_body=[])
            there = json.loads(
                json.dumps(plan).replace("http://homeassistant.local:8123", web.url(""))
            )
            check_plan(
                InstructionPlan.model_validate(there),
                manifest=skill.manifest,
                granted=skill.manifest.permissions,
                hosts=("127.0.0.1",),
                values=values,
                device_params={},
            )

            answer = run(
                there,
                context=Context(secrets={"HA_TOKEN": "tok"}, device={}, values=values),
                budget_s=10,
            )

        assert answer["message"] == "The movie scene is set."
        assert [(seen.method, seen.path) for seen in web.seen] == [
            ("POST", "/api/services/light/turn_off"),
            ("POST", "/api/services/light/turn_on"),
        ]
        assert web.seen[0].json() == {"entity_id": "light.living_room_ceiling"}
        assert web.seen[1].json() == {"entity_id": "light.tv_backlight", "brightness_pct": 20}
        assert {seen.headers["authorization"] for seen in web.seen} == {"Bearer tok"}

    def test_the_air_quality_is_looked_up_by_the_place_and_said_with_it(self) -> None:
        skill = examples()["air-quality"]
        plan, values = PLANS["air-quality"]
        with FakeWeb() as web:
            web.answer(
                "GET",
                "/v1/search",
                json_body={
                    "results": [{"name": "Rome", "latitude": 41.89193, "longitude": 12.51133}]
                },
            )
            web.answer(
                "GET",
                "/v1/air-quality",
                json_body={
                    "current": {"time": "2026-09-25T10:00", "interval": 3600, "european_aqi": 33}
                },
            )
            there = json.loads(
                json.dumps(plan)
                .replace("https://geocoding-api.open-meteo.com", web.url(""))
                .replace("https://air-quality-api.open-meteo.com", web.url(""))
            )
            check_plan(
                InstructionPlan.model_validate(there),
                manifest=skill.manifest,
                granted=skill.manifest.permissions,
                hosts=("127.0.0.1",),
                values=values,
                device_params={},
            )

            answer = run(there, context=Context(secrets={}, device={}, values=values), budget_s=10)

        assert answer["message"] == "The air quality index in Rome is 33."
        assert web.seen[0].query == "count=1&name=Rome"
        assert web.seen[1].query == "current=european_aqi&latitude=41.89193&longitude=12.51133"
