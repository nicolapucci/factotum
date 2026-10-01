"""The edge put together from its settings: what it refuses to start without, that it comes up with
the devices of its file and reaches the conversation service, and how it is started."""

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import uvicorn

from factotum.common.config import Environment
from factotum.common.tokens import MIN_SECRET_BYTES
from factotum.contracts.edge import MAX_FRAME_BYTES
from factotum.contracts.errors import AuthError
from factotum.edge import __main__ as entry
from factotum.edge.app import build_app
from factotum.edge.settings import EdgeSettings

DEVICES = """
devices:
  - name: Kitchen speaker
    hardware_id: kitchen-01
    token: kitchen-credential
    owner: tenant
    capabilities: [audio_in, audio_out]
"""

SECRET = "s" * MIN_SECRET_BYTES
HUB = "http://hub.test"


def settings_for(**values: Any) -> EdgeSettings:
    return EdgeSettings(_env_file=None, environment=Environment.TEST, **values)


@pytest.fixture
def devices_file(tmp_path: Path) -> Path:
    path = tmp_path / "devices.dev.yaml"
    path.write_text(DEVICES, encoding="utf-8")
    return path


class TestStartingUp:
    def test_it_does_not_start_in_production(self) -> None:
        with pytest.raises(AuthError, match="disabled"):
            build_app(EdgeSettings(_env_file=None, environment=Environment.PRODUCTION))

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_starts_in_development_and_in_tests(
        self, environment: Environment, devices_file: Path
    ) -> None:
        app = build_app(
            EdgeSettings(_env_file=None, environment=environment, edge_devices_file=devices_file)
        )

        assert {"/v1/connect", "/v1/devices", "/healthz", "/readyz"} <= {
            route.path  # type: ignore[attr-defined]
            for route in app.routes
        }

    def test_the_documentation_is_there_for_developers(self, devices_file: Path) -> None:
        app = build_app(settings_for(edge_devices_file=devices_file))

        assert app.docs_url == "/docs"

    def test_a_file_named_and_not_there_stops_it(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"nowhere\.yaml"):
            build_app(settings_for(edge_devices_file=tmp_path / "nowhere.yaml"))

    def test_a_file_of_devices_that_is_wrong_stops_it_and_says_where(self, tmp_path: Path) -> None:
        path = tmp_path / "devices.yaml"
        path.write_text("devices:\n  - {name: A}\n", encoding="utf-8")

        with pytest.raises(ValueError, match=r"devices\.yaml: device #1"):
            build_app(settings_for(edge_devices_file=path))

    def test_without_a_file_it_starts_and_says_that_no_device_can_connect(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="factotum.edge"):
            build_app(settings_for())

        assert "no device can connect" in caplog.text

    def test_it_starts_in_production_once_the_hub_is_configured(self) -> None:
        app = build_app(
            EdgeSettings(
                _env_file=None,
                environment=Environment.PRODUCTION,
                hub_token_secret=SECRET,  # type: ignore[arg-type]
                edge_hub_url=HUB,  # type: ignore[arg-type]
            )
        )

        assert {"/v1/connect", "/v1/devices", "/healthz", "/readyz"} <= {
            route.path  # type: ignore[attr-defined]
            for route in app.routes
        }

    def test_it_does_not_touch_the_file_of_devices_once_the_hub_is_configured(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="factotum.edge"):
            build_app(settings_for(hub_token_secret=SECRET, edge_hub_url=HUB))

        assert "no device can connect" not in caplog.text


class TestOnceItIsUp:
    @respx.mock
    async def test_it_is_ready_when_the_conversation_service_is(self, devices_file: Path) -> None:
        respx.get("http://conversation.test/readyz").respond(200, json={"status": "ok"})
        app = build_app(
            settings_for(
                edge_devices_file=devices_file, edge_conversation_url="http://conversation.test"
            )
        )

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://edge"
            ) as http,
        ):
            assert (await http.get("/readyz")).status_code == 200
            respx.get("http://conversation.test/readyz").respond(503)
            assert (await http.get("/readyz")).status_code == 503
            assert (await http.get("/v1/devices")).json() == {"devices": []}

    async def test_it_is_not_ready_when_the_conversation_service_is_not_there(
        self, devices_file: Path
    ) -> None:
        app = build_app(
            settings_for(edge_devices_file=devices_file, edge_conversation_url="http://127.0.0.1:1")
        )

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://edge"
            ) as http,
        ):
            assert (await http.get("/readyz")).status_code == 503


class TestBeingStarted:
    def test_it_serves_on_its_address_with_the_frames_the_protocol_allows(
        self, monkeypatch: pytest.MonkeyPatch, devices_file: Path
    ) -> None:
        monkeypatch.setenv("FACTOTUM_ENVIRONMENT", "test")
        monkeypatch.setenv("FACTOTUM_EDGE_DEVICES_FILE", str(devices_file))
        monkeypatch.setenv("FACTOTUM_EDGE_PORT", "8199")
        seen: dict[str, Any] = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **options: seen.update(options))
        monkeypatch.setattr(entry, "configure_logging", lambda **options: None)

        entry.main()

        assert (seen["host"], seen["port"]) == ("127.0.0.1", 8199)
        assert seen["ws_max_size"] == MAX_FRAME_BYTES
        assert seen["ws"] == "websockets-sansio"  # the implementation that is not deprecated
        assert seen["log_config"] is None  # its logs are the redacted ones

    def test_it_does_not_start_with_settings_that_are_not_valid(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("FACTOTUM_EDGE_PORT", "0")

        with pytest.raises(SystemExit) as stopped:
            entry.main()

        assert stopped.value.code == 2
        assert "the settings are not valid" in capsys.readouterr().err

    def test_it_does_not_start_where_it_may_not_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("FACTOTUM_ENVIRONMENT", raising=False)
        monkeypatch.setattr(entry, "configure_logging", lambda **options: None)

        with pytest.raises(SystemExit) as stopped:
            entry.main()

        assert stopped.value.code == 2
        assert "cannot start" in capsys.readouterr().err
