import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from factotum.common.config import Environment
from factotum.hub.settings import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[3]


def load(**values: object) -> HubSettings:
    return HubSettings(_env_file=None, **values)  # type: ignore[arg-type]


class TestDefaults:
    def test_it_listens_on_loopback_only_and_beside_the_other_services(self) -> None:
        settings = load()

        assert (settings.hub_host, settings.hub_port) == ("127.0.0.1", 8087)

    def test_left_alone_it_is_production_and_so_it_does_not_start(self) -> None:
        assert load().environment is Environment.PRODUCTION

    def test_the_bootstrap_administrator_is_admin_admin_until_told_otherwise(self) -> None:
        settings = load()

        assert (settings.hub_admin_username, settings.hub_admin_password) == ("admin", "admin")

    def test_a_session_lasts_half_a_day(self) -> None:
        assert load().hub_session_hours == 12

    def test_it_has_no_token_secret_until_one_is_set(self) -> None:
        assert load().hub_secret_bytes is None


class TestReadingTheEnvironment:
    def test_the_variables_are_the_field_names_with_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_HUB_HOST", "192.0.2.1")
        monkeypatch.setenv("FACTOTUM_HUB_PORT", "9000")
        monkeypatch.setenv("FACTOTUM_HUB_ADMIN_USERNAME", "root")
        monkeypatch.setenv("FACTOTUM_HUB_ADMIN_PASSWORD", "not-admin")
        monkeypatch.setenv("FACTOTUM_HUB_SESSION_HOURS", "1")
        monkeypatch.setenv("FACTOTUM_HUB_TOKEN_SECRET", "x" * 32)

        settings = load()

        assert (settings.hub_host, settings.hub_port) == ("192.0.2.1", 9000)
        assert (settings.hub_admin_username, settings.hub_admin_password) == ("root", "not-admin")
        assert settings.hub_session_hours == 1
        assert settings.hub_secret_bytes == b"x" * 32

    def test_the_database_is_the_one_compose_creates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FACTOTUM_POSTGRES_HOST", "postgres")
        monkeypatch.setenv("FACTOTUM_POSTGRES_PASSWORD", "secret")

        settings = load()

        assert settings.postgres_host == "postgres"
        assert settings.password() == "secret"


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "values",
        [
            {"hub_port": 0},
            {"hub_port": 70000},
            {"hub_session_hours": 0},
            {"hub_session_hours": 24 * 31},
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
    expected = {f"FACTOTUM_{name.upper()}" for name in HubSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"
