"""The runner put together from its settings: what it refuses to start without, that it sweeps what
a runner that died left, and that it is ready when the engine is."""

import asyncio

import httpx
import pytest

from factotum.common.config import Environment
from factotum.contracts.errors import AuthError
from factotum.runner import app as runner_app
from factotum.runner.app import build_app, engine_client, sweep
from factotum.runner.sandbox import Sandbox, SandboxSettings
from factotum.runner.settings import RunnerSettings
from factotum.runner.testing import FakeDocker


def settings_for(environment: Environment = Environment.TEST) -> RunnerSettings:
    return RunnerSettings(_env_file=None, environment=environment)


class TestStartingUp:
    def test_it_does_not_start_in_production(self) -> None:
        with pytest.raises(AuthError, match="disabled"):
            build_app(settings_for(Environment.PRODUCTION))

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_starts_in_development_and_in_tests(self, environment: Environment) -> None:
        app = build_app(settings_for(environment))

        paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
        assert {"/v1/jobs", "/healthz", "/readyz"} <= paths

    def test_the_documentation_is_there_for_developers(self) -> None:
        assert build_app(settings_for(Environment.DEVELOPMENT)).docs_url == "/docs"


class TestTheEngine:
    def test_a_socket_is_spoken_to_through_the_socket(self) -> None:
        client = engine_client("unix:///var/run/docker.sock")

        assert client.base_url == "http://docker"
        assert isinstance(client._transport, httpx.AsyncHTTPTransport)

    def test_a_port_is_spoken_to_on_its_address(self) -> None:
        client = engine_client("tcp://engine:2375")

        assert client.base_url == "http://engine:2375"


class TestWhileItRuns:
    @pytest.fixture
    def docker(self, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
        docker = FakeDocker()
        monkeypatch.setattr(runner_app, "engine_client", lambda host: docker.client())
        return docker

    async def test_it_is_ready_when_the_engine_answers_and_has_the_image(
        self, docker: FakeDocker
    ) -> None:
        app = build_app(settings_for())

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://r") as http,
        ):
            ready = await http.get("/readyz")
            docker.image_present = False
            not_ready = await http.get("/readyz")

        assert (ready.status_code, not_ready.status_code) == (200, 503)

    async def test_it_runs_a_job_on_the_engine_it_was_given(self, docker: FakeDocker) -> None:
        app = build_app(settings_for())
        job = {
            "label": "x",
            "files": [{"path": "run.py", "content": "cHJpbnQoMSk="}],
            "entrypoint": "run.py",
            "timeout_s": 5,
        }

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://r") as http,
        ):
            response = await http.post("/v1/jobs", json=job)

        assert response.status_code == 200
        assert docker.one().removed

    async def test_what_a_runner_that_died_left_is_swept_when_it_starts_and_from_time_to_time(
        self, docker: FakeDocker
    ) -> None:
        engine = docker.engine()
        labels = {"factotum.sandbox": "1"}
        left = await engine.create("left", {"Labels": labels})
        docker.containers[left].created -= 10_000

        app = build_app(settings_for())
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)

        assert docker.containers[left].removed

    async def test_the_sweep_goes_on_until_it_is_stopped(self) -> None:
        docker = FakeDocker()
        sandbox = Sandbox(docker.engine(), SandboxSettings())
        sweeping = asyncio.create_task(sweep(sandbox, every_s=0.01))
        await asyncio.sleep(0.05)
        docker.calls.clear()

        await asyncio.sleep(0.05)
        sweeping.cancel()

        assert docker.calls.count("list") >= 2
        with pytest.raises(asyncio.CancelledError):
            await sweeping
