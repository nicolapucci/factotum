import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from factotum.egress.settings import MIN_KEY_CHARS, EgressSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
KEY = "k" * MIN_KEY_CHARS


def load(**values: object) -> EgressSettings:
    return EgressSettings(_env_file=None, **values)  # type: ignore[arg-type]


class TestDefaults:
    def test_it_listens_on_loopback_only_beside_the_other_services(self) -> None:
        settings = load()

        assert (settings.egress_host, settings.egress_port) == ("127.0.0.1", 3128)

    def test_a_tunnel_is_made_to_the_port_of_https_only(self) -> None:
        assert load().egress_connect_ports == (443,)

    def test_there_is_no_key_until_one_is_given(self) -> None:
        settings = load()

        assert settings.egress_key is None
        with pytest.raises(ValueError, match="FACTOTUM_EGRESS_KEY is not set"):
            _ = settings.key

    def test_the_key_is_bytes_and_never_shown(self) -> None:
        settings = load(egress_key=KEY)

        assert settings.key == KEY.encode()
        assert KEY not in repr(settings)
        assert KEY not in str(settings.model_dump())


class TestReadingTheEnvironment:
    def test_the_variables_are_the_field_names_with_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_EGRESS_HOST", "0.0.0.0")  # noqa: S104 - what a container says
        monkeypatch.setenv("FACTOTUM_EGRESS_PORT", "8888")
        monkeypatch.setenv("FACTOTUM_EGRESS_KEY", KEY)
        monkeypatch.setenv("FACTOTUM_EGRESS_CONNECT_PORTS", "443, 8443")
        monkeypatch.setenv("FACTOTUM_EGRESS_MAX_MB", "5")

        settings = load()

        assert (settings.egress_host, settings.egress_port) == ("0.0.0.0", 8888)  # noqa: S104
        assert settings.egress_connect_ports == (443, 8443)
        assert settings.egress_max_mb == 5
        assert settings.key == KEY.encode()

    def test_a_single_port_is_a_list_of_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FACTOTUM_EGRESS_CONNECT_PORTS", "8443")

        assert load().egress_connect_ports == (8443,)


class TestWhatIsRefused:
    def test_a_key_that_is_too_short_to_be_one(self) -> None:
        with pytest.raises(ValidationError, match="at least 32 characters"):
            load(egress_key="k" * (MIN_KEY_CHARS - 1))

    @pytest.mark.parametrize("ports", ["", ",", "0", "65536", "-1", "https"])
    def test_ports_that_are_not_ports(self, ports: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FACTOTUM_EGRESS_CONNECT_PORTS", ports)

        with pytest.raises((ValidationError, ValueError)):
            load()

    @pytest.mark.parametrize(
        "values",
        [
            {"egress_port": 0},
            {"egress_max_connections": 0},
            {"egress_header_timeout_s": 0},
            {"egress_connect_timeout_s": 121},
            {"egress_idle_timeout_s": 0},
            {"egress_max_duration_s": 0},
            {"egress_max_mb": 0},
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
    expected = {f"FACTOTUM_{name.upper()}" for name in EgressSettings.model_fields}

    assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"
