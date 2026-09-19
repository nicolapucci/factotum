import os
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import ValidationError

from factotum.common.config import Environment
from factotum.common.devauth import SUBJECT_HEADER
from factotum.contracts.errors import AuthError
from factotum.model_gateway import __main__ as entrypoint
from factotum.model_gateway.app import build_app, build_gateway
from factotum.model_gateway.settings import GatewaySettings

REPO_ROOT = Path(__file__).resolve().parents[3]

BINDINGS = """
bindings:
  - role: chat.main
    provider: ollama
    model: llama3.1:8b
    base_url: http://localhost:11434
    options:
      max_input_tokens: 16000
"""


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neither the developer's FACTOTUM_* variables nor their .env may leak into these tests."""
    for name in [name for name in os.environ if name.startswith("FACTOTUM_")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def settings(**kwargs: Any) -> GatewaySettings:
    return GatewaySettings(_env_file=None, **kwargs)


def bindings_file(tmp_path: Path, text: str = BINDINGS) -> Path:
    path = tmp_path / "bindings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestSettings:
    def test_the_defaults_keep_the_gateway_to_itself(self) -> None:
        defaults = settings()

        assert defaults.gateway_host == "127.0.0.1"
        assert defaults.gateway_port == 8081
        assert defaults.gateway_bindings_file is None
        assert defaults.environment is Environment.PRODUCTION

    def test_values_are_read_from_prefixed_environment_variables(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("FACTOTUM_GATEWAY_HOST", "0.0.0.0")  # noqa: S104 - the point of the test
        monkeypatch.setenv("FACTOTUM_GATEWAY_PORT", "9000")
        monkeypatch.setenv("FACTOTUM_GATEWAY_BINDINGS_FILE", str(tmp_path / "b.yaml"))

        loaded = settings()

        assert loaded.gateway_host == "0.0.0.0"  # noqa: S104
        assert loaded.gateway_port == 9000
        assert loaded.gateway_bindings_file == tmp_path / "b.yaml"

    @pytest.mark.parametrize("port", [0, 70000, -1])
    def test_a_port_that_cannot_exist_is_refused(self, port: int) -> None:
        with pytest.raises(ValidationError):
            settings(gateway_port=port)

    def test_every_setting_is_documented_in_the_env_example(self) -> None:
        documented = set(
            re.findall(
                r"^#?\s*(FACTOTUM_[A-Z0-9_]+)=",
                (REPO_ROOT / ".env.example").read_text("utf-8"),
                re.M,
            )
        )
        expected = {f"FACTOTUM_{name.upper()}" for name in GatewaySettings.model_fields}

        assert expected <= documented, f"missing from .env.example: {sorted(expected - documented)}"


class TestBuilding:
    def test_in_production_there_is_no_way_to_authenticate_yet_and_it_does_not_start(self) -> None:
        with pytest.raises(AuthError, match="disabled outside"):
            build_app(settings(environment=Environment.PRODUCTION))

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    async def test_in_development_and_test_it_serves(self, environment: Environment) -> None:
        app = build_app(settings(environment=environment))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            response = await client.get("/healthz")

        assert response.status_code == 200

    async def test_the_docs_are_served_in_development(self) -> None:
        app = build_app(settings(environment=Environment.DEVELOPMENT))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            response = await client.get("/docs")

        assert response.status_code == 200

    async def test_the_bindings_of_the_file_serve_the_development_tenant(
        self, tmp_path: Path
    ) -> None:
        app = build_app(
            settings(
                environment=Environment.DEVELOPMENT, gateway_bindings_file=bindings_file(tmp_path)
            )
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            as_user = await client.get("/v1/profiles/chat.main")
            as_tenant = await client.get(
                "/v1/profiles/chat.main", headers={SUBJECT_HEADER: "tenant"}
            )

        assert as_user.status_code == as_tenant.status_code == 200
        assert as_user.json()["max_input_tokens"] == 16000
        assert as_user.json()["locality"] == "local"

    async def test_without_a_file_nothing_is_bound(self) -> None:
        app = build_app(settings(environment=Environment.DEVELOPMENT))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            response = await client.get("/v1/profiles/chat.main")

        assert response.status_code == 404

    def test_a_missing_bindings_file_stops_the_start(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            build_gateway(settings(gateway_bindings_file=tmp_path / "nope.yaml"))

    def test_a_bindings_file_with_a_mistake_stops_the_start_and_says_where(
        self, tmp_path: Path
    ) -> None:
        broken = bindings_file(tmp_path, "bindings:\n  - {role: chat.main}\n")

        with pytest.raises(ValueError, match=r"bindings\.yaml: binding #1"):
            build_gateway(settings(gateway_bindings_file=broken))

    def test_two_bindings_of_one_role_for_the_tenant_stop_the_start(self, tmp_path: Path) -> None:
        twice = bindings_file(tmp_path, BINDINGS + BINDINGS.replace("bindings:\n", ""))

        with pytest.raises(ValueError, match="already has a binding"):
            build_gateway(settings(gateway_bindings_file=twice))


class TestEntryPoint:
    @staticmethod
    def capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def run(app: Any, **kwargs: Any) -> None:
            captured["app"] = app
            captured.update(kwargs)

        def configure(**kwargs: Any) -> None:
            captured["logging"] = kwargs

        monkeypatch.setattr(uvicorn, "run", run)
        monkeypatch.setattr(entrypoint, "configure_logging", configure)
        return captured

    def test_it_serves_the_app_where_the_settings_say(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self.capture(monkeypatch)
        monkeypatch.setenv("FACTOTUM_ENVIRONMENT", "development")
        monkeypatch.setenv("FACTOTUM_GATEWAY_HOST", "0.0.0.0")  # noqa: S104
        monkeypatch.setenv("FACTOTUM_GATEWAY_PORT", "9001")

        entrypoint.main()

        assert captured["host"] == "0.0.0.0"  # noqa: S104
        assert captured["port"] == 9001
        assert captured["app"] is not None

    def test_uvicorn_does_not_replace_the_redacting_logging(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self.capture(monkeypatch)
        monkeypatch.setenv("FACTOTUM_ENVIRONMENT", "development")

        entrypoint.main()

        assert captured["log_config"] is None

    def test_logging_is_configured_from_the_settings_before_anything_else(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self.capture(monkeypatch)
        monkeypatch.setenv("FACTOTUM_ENVIRONMENT", "development")
        monkeypatch.setenv("FACTOTUM_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("FACTOTUM_LOG_JSON", "false")

        entrypoint.main()

        assert captured["logging"] == {"level": "DEBUG", "json_format": False}

    def test_in_production_it_says_why_it_cannot_start_and_exits(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        captured = self.capture(monkeypatch)

        with pytest.raises(SystemExit) as caught:
            entrypoint.main()

        assert caught.value.code == 2
        assert "cannot start" in capsys.readouterr().err
        assert "app" not in captured

    def test_a_bad_bindings_file_is_reported_without_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        captured = self.capture(monkeypatch)
        monkeypatch.setenv("FACTOTUM_ENVIRONMENT", "development")
        monkeypatch.setenv("FACTOTUM_GATEWAY_BINDINGS_FILE", str(tmp_path / "missing.yaml"))

        with pytest.raises(SystemExit) as caught:
            entrypoint.main()

        assert caught.value.code == 2
        assert "missing.yaml" in capsys.readouterr().err
        assert "app" not in captured
