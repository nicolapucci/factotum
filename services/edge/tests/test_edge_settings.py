import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from factotum.common.config import Environment
from factotum.contracts.edge import HEARTBEAT_SECONDS
from factotum.edge.session import SessionSettings
from factotum.edge.settings import EdgeSettings

REPO_ROOT = Path(__file__).resolve().parents[3]


def load(**values: object) -> EdgeSettings:
    return EdgeSettings(_env_file=None, **values)  # type: ignore[arg-type]


class TestDefaults:
    def test_it_listens_on_loopback_only_and_beside_the_other_services(self) -> None:
        settings = load()

        assert (settings.edge_host, settings.edge_port) == ("127.0.0.1", 8084)

    def test_the_conversation_is_where_it_listens_when_started_by_hand(self) -> None:
        assert str(load().edge_conversation_url) == "http://127.0.0.1:8083/"

    def test_no_device_is_known_until_a_file_says_which(self) -> None:
        assert load().edge_devices_file is None

    def test_left_alone_it_is_production_and_so_it_does_not_start(self) -> None:
        assert load().environment is Environment.PRODUCTION

    def test_a_device_may_be_quiet_for_a_good_few_heartbeats_before_it_is_gone(self) -> None:
        settings = load()

        assert settings.edge_heartbeat_s == HEARTBEAT_SECONDS
        assert settings.edge_idle_timeout_s >= 2 * settings.edge_heartbeat_s

    def test_the_conversation_is_waited_for_longer_than_it_waits_for_its_model(self) -> None:
        # The conversation service waits 120 s for its model by default.
        assert load().edge_turn_timeout_s > 120

    def test_a_stream_of_audio_is_dropped_long_before_its_device_is_idle(self) -> None:
        settings = load()

        assert settings.edge_stream_stall_s == 5
        assert settings.edge_stream_stall_s < settings.edge_idle_timeout_s / 2


class TestReadingTheEnvironment:
    def test_the_variables_are_the_field_names_with_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        devices = tmp_path / "devices.yaml"
        monkeypatch.setenv("FACTOTUM_EDGE_HOST", "0.0.0.0")  # noqa: S104 - a value under test
        monkeypatch.setenv("FACTOTUM_EDGE_PORT", "9000")
        monkeypatch.setenv("FACTOTUM_EDGE_CONVERSATION_URL", "http://conversation:8083")
        monkeypatch.setenv("FACTOTUM_EDGE_DEVICES_FILE", str(devices))
        monkeypatch.setenv("FACTOTUM_EDGE_HELLO_TIMEOUT_S", "3")
        monkeypatch.setenv("FACTOTUM_EDGE_HEARTBEAT_S", "10")
        monkeypatch.setenv("FACTOTUM_EDGE_IDLE_TIMEOUT_S", "45")
        monkeypatch.setenv("FACTOTUM_EDGE_COMMAND_TIMEOUT_S", "5")
        monkeypatch.setenv("FACTOTUM_EDGE_TURN_TIMEOUT_S", "200")
        monkeypatch.setenv("FACTOTUM_EDGE_SEND_TIMEOUT_S", "2")

        settings = load()

        assert (settings.edge_host, settings.edge_port) == ("0.0.0.0", 9000)  # noqa: S104
        assert str(settings.edge_conversation_url) == "http://conversation:8083/"
        assert settings.edge_devices_file == devices

    def test_what_a_session_waits_for_is_what_the_settings_say(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_EDGE_HELLO_TIMEOUT_S", "3")
        monkeypatch.setenv("FACTOTUM_EDGE_HEARTBEAT_S", "10")
        monkeypatch.setenv("FACTOTUM_EDGE_IDLE_TIMEOUT_S", "45")
        monkeypatch.setenv("FACTOTUM_EDGE_COMMAND_TIMEOUT_S", "5")
        monkeypatch.setenv("FACTOTUM_EDGE_SEND_TIMEOUT_S", "2")
        monkeypatch.setenv("FACTOTUM_EDGE_STREAM_STALL_S", "1.5")

        session = SessionSettings.of(load())

        assert (
            session.hello_timeout_s,
            session.heartbeat_s,
            session.idle_timeout_s,
            session.command_timeout_s,
            session.send_timeout_s,
            session.stream_stall_s,
        ) == (3, 10, 45, 5, 2, 1.5)


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "values",
        [
            {"edge_port": 0},
            {"edge_port": 70000},
            {"edge_hello_timeout_s": 0},
            {"edge_heartbeat_s": 0},
            {"edge_idle_timeout_s": 0},
            {"edge_idle_timeout_s": 7200},
            {"edge_command_timeout_s": -1},
            {"edge_turn_timeout_s": 0},
            {"edge_send_timeout_s": 0},
            {"edge_stream_stall_s": 0},
            {"edge_stream_stall_s": 61},
        ],
    )
    def test_a_number_outside_what_makes_sense(self, values: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            load(**values)


def test_every_setting_of_the_service_is_documented_in_the_env_example() -> None:
    documented = set(
        re.findall(
            r"^#?\s*(FACTOTUM_[A-Z0-9_]+)=", (REPO_ROOT / ".env.example").read_text("utf-8"), re.M
        )
    )
    expected = {f"FACTOTUM_{name.upper()}" for name in EdgeSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"
