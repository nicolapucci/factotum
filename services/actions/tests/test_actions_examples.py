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

from factotum.actions.runner_client import hosts_of, read_output
from factotum.actions.skills import LoadedSkill, load_folder
from factotum.contracts.commands import SetVolume
from factotum.contracts.effects import Effect
from factotum.contracts.runner import JobResult, RunResult
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
        return calls, read_output(name, JobResult(exit_code=0, stdout=printed))

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
        calls, result = self.run(
            "weather",
            {"params": {"place": "São Paulo & co"}, "device_params": {}},
            monkeypatch,
            capsys,
            answers=[{}],
        )

        assert calls[0].endswith("&name=S%C3%A3o%20Paulo%20%26%20co")
        assert result.message == "I could not find São Paulo & co."

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
        for name, skill in examples().items():
            source = skill.files["scripts/run.py"].decode()
            asked = set(re.findall(r"os\.environ\[['\"]([A-Z_]+)['\"]\]", source))
            assert asked == set(skill.manifest.permissions.secrets), name
