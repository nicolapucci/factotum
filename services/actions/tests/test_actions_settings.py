import re
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from factotum.actions.service import ActionSettings
from factotum.actions.settings import ActionsSettings
from factotum.common.config import Environment

REPO_ROOT = Path(__file__).resolve().parents[3]


def load(**values: object) -> ActionsSettings:
    return ActionsSettings(_env_file=None, **values)  # type: ignore[arg-type]


class TestDefaults:
    def test_it_listens_on_loopback_only_and_beside_the_other_services(self) -> None:
        settings = load()

        assert (settings.actions_host, settings.actions_port) == ("127.0.0.1", 8085)

    def test_the_gateway_is_where_it_listens_when_started_by_hand(self) -> None:
        assert str(load().actions_gateway_url) == "http://127.0.0.1:8081/"

    def test_left_alone_it_is_production_and_so_it_does_not_start(self) -> None:
        assert load().environment is Environment.PRODUCTION

    def test_it_knows_no_devices_and_no_folder_of_skills_and_enables_nothing_by_reading(
        self,
    ) -> None:
        settings = load()

        assert settings.actions_devices_file is None
        assert settings.actions_skills_dir is None
        assert settings.actions_enable_skills_dir is False

    def test_the_action_settings_are_the_ones_the_service_is_written_with(self) -> None:
        assert load().action == ActionSettings()

    def test_a_confirmation_is_waited_for_a_minute_and_an_approval_stays_open_ten(self) -> None:
        action = load().action

        assert action.confirmation_ttl == timedelta(seconds=60)
        assert action.approval_ttl == timedelta(minutes=10)


class TestReadingTheEnvironment:
    def test_the_variables_are_the_field_names_with_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_ACTIONS_GATEWAY_URL", "http://model-gateway:8081")
        monkeypatch.setenv("FACTOTUM_ACTIONS_DEVICES_FILE", "/etc/factotum/devices.dev.yaml")
        monkeypatch.setenv("FACTOTUM_ACTIONS_SKILLS_DIR", "/skills")
        monkeypatch.setenv("FACTOTUM_ACTIONS_MATCH_THRESHOLD", "0.6")
        monkeypatch.setenv("FACTOTUM_ACTIONS_CONFIRMATION_SECONDS", "30")
        monkeypatch.setenv("FACTOTUM_ACTIONS_APPROVAL_MINUTES", "5")
        monkeypatch.setenv("FACTOTUM_ACTIONS_MAX_DEVICES_SHOWN", "4")

        settings = load()

        assert str(settings.actions_gateway_url) == "http://model-gateway:8081/"
        assert settings.actions_devices_file == Path("/etc/factotum/devices.dev.yaml")
        assert settings.actions_skills_dir == Path("/skills")
        assert settings.actions_match_threshold == 0.6
        assert settings.action.confirmation_ttl == timedelta(seconds=30)
        assert settings.action.approval_ttl == timedelta(minutes=5)
        assert settings.action.max_devices_shown == 4

    def test_the_database_is_the_one_compose_creates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FACTOTUM_POSTGRES_HOST", "postgres")
        monkeypatch.setenv("FACTOTUM_POSTGRES_PASSWORD", "secret")

        settings = load()

        assert settings.postgres_host == "postgres"
        assert settings.password() == "secret"


class TestWhatIsRefused:
    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_reading_a_folder_enables_it_in_development_and_in_tests(
        self, environment: Environment
    ) -> None:
        assert load(environment=environment, actions_enable_skills_dir=True)

    def test_reading_a_folder_never_enables_it_in_production(self) -> None:
        with pytest.raises(ValidationError, match="reviewed and enabled"):
            load(environment=Environment.PRODUCTION, actions_enable_skills_dir=True)

    def test_reading_a_folder_never_enables_it_when_the_environment_is_not_said(self) -> None:
        with pytest.raises(ValidationError, match="reviewed and enabled"):
            load(actions_enable_skills_dir=True)

    @pytest.mark.parametrize(
        "values",
        [
            {"actions_port": 0},
            {"actions_match_threshold": 1.5},
            {"actions_match_threshold": -0.1},
            {"actions_match_candidates": 0},
            {"actions_match_timeout_s": 0},
            {"actions_index_timeout_s": 0},
            {"actions_planner_timeout_s": 121},
            {"actions_reader_timeout_s": 0},
            {"actions_confirmation_seconds": 4},
            {"actions_approval_wait_s": -1},
            {"actions_approval_minutes": 0},
            {"actions_run_margin_s": 61},
            {"actions_max_devices_shown": 0},
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
    expected = {f"FACTOTUM_{name.upper()}" for name in ActionsSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"
