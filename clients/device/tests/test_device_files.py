"""The files that go with the application: the settings to start from, and the unit that starts it
with a machine. What they say has to be what the code does, or they are a trap that a person falls
into the first time they copy one."""

import configparser
import shlex
import tomllib
from pathlib import Path

from pydantic import BaseModel

from factotum.client_audio import ACCESS_KEY_VARIABLE
from factotum.client_core import Ending
from factotum.contracts.capabilities import Capability
from factotum.device.cli import EXAMPLE, EXIT_CODES
from factotum.device.config import (
    CONFIG_VARIABLE,
    TOKEN_VARIABLE,
    AudioSettings,
    Config,
    DeviceSettings,
    EdgeSettings,
    ListeningSettings,
    Mode,
    ReconnectSettings,
    parse_config,
)

ROOT = Path(__file__).resolve().parents[3]
SETTINGS = ROOT / "config" / "device.example.yaml"
UNIT = ROOT / "infra" / "device" / "factotum-device.service"
SECTIONS: dict[str, type[BaseModel]] = {
    "edge": EdgeSettings,
    "device": DeviceSettings,
    "audio": AudioSettings,
    "listening": ListeningSettings,
    "reconnect": ReconnectSettings,
}
PAIRED = {TOKEN_VARIABLE: "the-credential"}


class TestTheExampleSettings:
    def test_it_is_what_init_writes(self) -> None:
        assert SETTINGS.read_bytes().decode("utf-8") == EXAMPLE

    def test_it_is_a_device_that_works_when_it_is_given_its_credential(self) -> None:
        config = parse_config(EXAMPLE, PAIRED)

        assert config.device.hardware_id == "kitchen-01"
        assert config.device.capabilities == {Capability.AUDIO_IN, Capability.AUDIO_OUT}
        assert config.listening.mode is Mode.PUSH_TO_TALK

    def test_it_says_what_every_setting_of_the_application_is(self) -> None:
        assert set(Config.model_fields) == {*SECTIONS, "log_level"}, "a section the file lacks"
        for section, model in SECTIONS.items():
            assert f"\n{section}:" in f"\n{EXAMPLE}", section
            for name in model.model_fields:
                assert f"{name}:" in EXAMPLE, f"{section}.{name} is not in the example"
        assert "log_level:" in EXAMPLE

    def test_what_it_shows_is_what_a_device_does_when_it_is_not_told(self) -> None:
        bare = parse_config("device:\n  hardware_id: kitchen-01\n", PAIRED)

        assert parse_config(EXAMPLE, PAIRED) == bare


class TestTheUnit:
    def unit(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(delimiters=("=",), interpolation=None, strict=True)
        parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive
        parser.read_string(UNIT.read_text("utf-8"))
        return parser

    def test_it_runs_the_command_that_the_package_installs(self) -> None:
        scripts = tomllib.loads((ROOT / "clients/device/pyproject.toml").read_text("utf-8"))[
            "project"
        ]["scripts"]

        program, *arguments = shlex.split(self.unit()["Service"]["ExecStart"])

        assert Path(program).name in scripts
        assert arguments == ["run"]

    def test_it_reads_the_settings_where_the_application_looks_and_keeps_the_secrets_out(
        self,
    ) -> None:
        unit = self.unit()
        service = unit["Service"]
        settings = " ".join(
            value for section in unit.sections() for value in unit[section].values()
        )

        assert service["Environment"].startswith(f"{CONFIG_VARIABLE}=")
        assert service["EnvironmentFile"].endswith("device.env")
        for secret in (TOKEN_VARIABLE, ACCESS_KEY_VARIABLE):
            assert secret not in settings  # the comments say where they go, and the unit has none

    def test_it_restarts_what_failed_and_not_what_the_edge_decided(self) -> None:
        service = self.unit()["Service"]
        decided = {
            EXIT_CODES[ending]
            for ending in (Ending.REFUSED, Ending.REPLACED, Ending.UNSUPPORTED, Ending.CLOSED)
        }

        assert service["Restart"] == "on-failure"
        assert {int(code) for code in service["RestartPreventExitStatus"].split()} == decided
        assert 0 not in decided
        assert 1 not in decided  # settings and sound cards that fail are tried again

    def test_it_waits_for_the_network_and_the_sound_card(self) -> None:
        unit = self.unit()["Unit"]

        assert "network-online.target" in unit["After"]
        assert "sound.target" in unit["After"]

    def test_it_runs_as_a_user_of_its_own_that_may_use_the_sound_card(self) -> None:
        service = self.unit()["Service"]

        assert service["User"] != "root"
        assert "audio" in service["SupplementaryGroups"].split()
