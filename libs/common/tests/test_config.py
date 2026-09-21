import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.identity import TenantMode

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own FACTOTUM_* variables must not leak into these tests."""
    import os

    for name in [name for name in os.environ if name.startswith("FACTOTUM_")]:
        monkeypatch.delenv(name)


def load(**kwargs: object) -> ServiceSettings:
    return ServiceSettings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_the_defaults_are_the_safe_ones() -> None:
    settings = load()

    assert settings.environment is Environment.PRODUCTION
    assert settings.is_production
    assert settings.tenant_mode is TenantMode.SINGLE_TENANT
    assert settings.log_level == "INFO"
    assert settings.log_json is True


def test_values_are_read_from_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACTOTUM_ENVIRONMENT", "development")
    monkeypatch.setenv("FACTOTUM_TENANT_MODE", "multi_tenant")
    monkeypatch.setenv("FACTOTUM_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("FACTOTUM_LOG_JSON", "false")

    settings = load()

    assert settings.environment is Environment.DEVELOPMENT
    assert not settings.is_production
    assert settings.tenant_mode is TenantMode.MULTI_TENANT
    assert settings.log_level == "DEBUG"
    assert settings.log_json is False


def test_unprefixed_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")

    assert load().environment is Environment.PRODUCTION


def test_a_dotenv_file_is_read(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("FACTOTUM_LOG_LEVEL=WARNING\nFACTOTUM_UNKNOWN=ignored\n", encoding="utf-8")

    settings = ServiceSettings(_env_file=dotenv)

    assert settings.log_level == "WARNING"


def test_a_test_runs_where_the_env_file_of_a_developer_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stack that is up has a ``.env`` in the repository (its passwords, its keys, the
    environment): a unit test must not find it by looking where it is run."""
    monkeypatch.delenv("FACTOTUM_ENVIRONMENT", raising=False)
    assert not Path(".env").exists()
    assert not any(Path.cwd().iterdir())
    assert ServiceSettings().environment is Environment.PRODUCTION

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FACTOTUM_ENVIRONMENT=development\n", encoding="utf-8")
    assert ServiceSettings().environment is Environment.DEVELOPMENT


def test_the_environment_wins_over_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("FACTOTUM_LOG_LEVEL=WARNING\n", encoding="utf-8")
    monkeypatch.setenv("FACTOTUM_LOG_LEVEL", "ERROR")

    assert ServiceSettings(_env_file=dotenv).log_level == "ERROR"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FACTOTUM_TENANT_MODE", "many_tenants"),
        ("FACTOTUM_LOG_LEVEL", "LOUD"),
        ("FACTOTUM_ENVIRONMENT", "staging"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        load()


def test_settings_are_immutable() -> None:
    settings = load()

    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_a_service_can_extend_the_settings_and_keep_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GatewaySettings(ServiceSettings):
        request_timeout_s: float = 30.0

    monkeypatch.setenv("FACTOTUM_REQUEST_TIMEOUT_S", "5")

    settings = GatewaySettings(_env_file=None)

    assert settings.request_timeout_s == 5.0
    assert settings.log_level == "INFO"


def test_every_setting_is_documented_in_the_env_example() -> None:
    documented = set(
        re.findall(
            r"^#?\s*(FACTOTUM_[A-Z0-9_]+)=", (REPO_ROOT / ".env.example").read_text("utf-8"), re.M
        )
    )
    expected = {f"FACTOTUM_{name.upper()}" for name in ServiceSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"
