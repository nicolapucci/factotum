import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from factotum.db.settings import DatabaseNotConfiguredError, DatabaseSettings
from factotum.db.testing import REQUIRE_VARIABLE, server_settings, skip_or_fail

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("FACTOTUM_")]:
        monkeypatch.delenv(name)


def settings(**kwargs: object) -> DatabaseSettings:
    return DatabaseSettings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_the_defaults_point_at_the_database_compose_starts() -> None:
    defaults = settings()

    assert (defaults.postgres_host, defaults.postgres_port) == ("127.0.0.1", 5432)
    assert (defaults.postgres_user, defaults.postgres_db) == ("factotum", "factotum")
    assert defaults.postgres_pool_max == 10


def test_values_come_from_the_variables_compose_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTOTUM_POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("FACTOTUM_POSTGRES_PORT", "6543")
    monkeypatch.setenv("FACTOTUM_POSTGRES_USER", "app")
    monkeypatch.setenv("FACTOTUM_POSTGRES_DB", "things")
    monkeypatch.setenv("FACTOTUM_POSTGRES_PASSWORD", "s3cret-value")

    loaded = settings()

    assert (loaded.postgres_host, loaded.postgres_port) == ("db.internal", 6543)
    assert (loaded.postgres_user, loaded.postgres_db) == ("app", "things")
    assert loaded.password() == "s3cret-value"


def test_there_is_no_default_password() -> None:
    with pytest.raises(DatabaseNotConfiguredError, match="FACTOTUM_POSTGRES_PASSWORD"):
        settings().password()


def test_an_empty_password_is_no_password() -> None:
    with pytest.raises(DatabaseNotConfiguredError):
        settings(postgres_password="").password()


def test_the_password_never_shows_in_a_repr_or_a_dump() -> None:
    loaded = settings(postgres_password="s3cret-value")

    assert "s3cret-value" not in repr(loaded)
    assert "s3cret-value" not in str(loaded.model_dump())


@pytest.mark.parametrize("port", [0, 70000])
def test_a_port_that_cannot_exist_is_refused(port: int) -> None:
    with pytest.raises(ValidationError):
        settings(postgres_port=port)


def test_the_pool_has_bounds() -> None:
    with pytest.raises(ValidationError):
        settings(postgres_pool_max=0)


def test_the_test_harness_reads_the_environment_and_not_a_dotenv_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("FACTOTUM_POSTGRES_HOST=from-the-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert server_settings().postgres_host == "127.0.0.1"


def test_every_setting_is_documented_in_the_env_example() -> None:
    documented = set(
        re.findall(
            r"^#?\s*(FACTOTUM_[A-Z0-9_]+)=", (REPO_ROOT / ".env.example").read_text("utf-8"), re.M
        )
    )
    expected = {f"FACTOTUM_{name.upper()}" for name in DatabaseSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"


def test_without_a_database_the_integration_tests_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(REQUIRE_VARIABLE, raising=False)

    with pytest.raises(pytest.skip.Exception, match="docker compose up"):
        skip_or_fail(OSError("refused"))


def test_where_a_database_is_required_its_absence_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REQUIRE_VARIABLE, "1")

    with pytest.raises(pytest.fail.Exception, match="OSError"):
        skip_or_fail(OSError("refused"))
