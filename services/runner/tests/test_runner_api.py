"""The HTTP interface of the runner, over a sandbox that is made on an engine of Python."""

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevAuthenticator
from factotum.contracts.runner import JobFile, JobResult
from factotum.contracts.testing import make_tenant_id, make_user_id
from factotum.runner.api import MAX_BODY_BYTES, create_app
from factotum.runner.sandbox import Sandbox, SandboxSettings
from factotum.runner.testing import FakeDocker, Fate


def app_of(docker: FakeDocker, *, docs: bool = False, **settings: Any) -> FastAPI:
    sandbox = Sandbox(
        docker.engine(), SandboxSettings(egress_key=b"r" * 32, queue_wait_s=0.1, **settings)
    )
    authenticator = DevAuthenticator(
        ServiceSettings(_env_file=None, environment=Environment.TEST),
        tenant_id=make_tenant_id(),
        user_id=make_user_id(),
    )

    async def ready() -> bool:
        return await sandbox.ready()

    return create_app(authenticator, sandbox=sandbox, docs=docs, ready=ready)


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runner")


def job_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "label": "lamp",
        "files": [JobFile.of("scripts/run.py", b"print('{}')").model_dump()],
        "entrypoint": "scripts/run.py",
        "stdin": "{}",
        "timeout_s": 5,
    }
    return {**body, **changes}


def error_code(response: httpx.Response) -> str:
    code: str = response.json()["error"]["code"]
    return code


class TestRunningAJob:
    async def test_what_the_script_did_is_the_answer(self) -> None:
        docker = FakeDocker(Fate(exit_code=0, stdout=b'{"message": "on"}', stderr=b"note"))

        async with client_of(app_of(docker)) as http:
            response = await http.post("/v1/jobs", json=job_body())

        result = JobResult.model_validate(response.json())
        assert response.status_code == 200
        assert (result.exit_code, result.stdout, result.stderr) == (0, '{"message": "on"}', "note")

    async def test_a_script_that_failed_is_an_answer_and_not_an_error(self) -> None:
        docker = FakeDocker(Fate(exit_code=2, stderr=b"it broke"))

        async with client_of(app_of(docker)) as http:
            response = await http.post("/v1/jobs", json=job_body())

        assert response.status_code == 200
        assert response.json()["exit_code"] == 2

    async def test_a_script_that_was_too_slow_is_an_answer_too(self) -> None:
        docker = FakeDocker(Fate(hangs=True))

        async with client_of(app_of(docker)) as http:
            response = await http.post("/v1/jobs", json=job_body(timeout_s=1))

        assert response.status_code == 200
        assert response.json()["timed_out"] is True
        assert response.json()["exit_code"] is None

    async def test_the_job_is_run_with_what_it_says_and_nothing_it_does_not(self) -> None:
        docker = FakeDocker()

        async with client_of(app_of(docker)) as http:
            await http.post(
                "/v1/jobs",
                json=job_body(env={"HA_TOKEN": "s3cret-value"}, hosts=["homeassistant.local"]),
            )

        body = docker.one().body
        assert "HA_TOKEN=s3cret-value" in body["Env"]
        assert body["HostConfig"]["NetworkMode"] == "factotum-sandbox"


class TestWhenItCannotBeRun:
    async def test_an_engine_that_is_not_there_is_a_service_that_is_not_available(self) -> None:
        docker = FakeDocker()
        docker.down = True

        async with client_of(app_of(docker)) as http:
            response = await http.post("/v1/jobs", json=job_body())

        assert (response.status_code, error_code(response)) == (503, "runner_unavailable")
        assert "engine" in response.json()["error"]["message"]

    async def test_a_job_that_the_engine_refused_is_the_same_without_what_it_said(self) -> None:
        docker = FakeDocker()
        docker.fail["create"] = 500

        async with client_of(app_of(docker)) as http:
            response = await http.post("/v1/jobs", json=job_body())

        assert (response.status_code, error_code(response)) == (503, "runner_unavailable")
        assert "create failed" not in response.text

    async def test_when_the_runner_is_busy_the_next_is_told_to_try_again_later(self) -> None:
        docker = FakeDocker(Fate(hangs=True))
        app = app_of(docker, max_concurrency=1)

        async with client_of(app) as http:
            first = asyncio.create_task(http.post("/v1/jobs", json=job_body(timeout_s=1)))
            await asyncio.wait_for(docker.wait_started.wait(), 5)
            second = await http.post("/v1/jobs", json=job_body())
            await first

        assert (second.status_code, error_code(second)) == (503, "runner_unavailable")
        assert "busy" in second.json()["error"]["message"]


class TestWhatIsNotAJob:
    @pytest.mark.parametrize(
        "changes",
        [
            {"entrypoint": "scripts/other.py"},
            {"entrypoint": "run.sh", "files": [JobFile.of("run.sh", b"x").model_dump()]},
            {
                "files": [{"path": "../outside.py", "content": "eA=="}],
                "entrypoint": "../outside.py",
            },
            {"files": []},
            {"env": {"PATH": "/evil"}},
            {"env": {"HTTPS_PROXY": "http://evil"}},
            {"hosts": ["http://a.org"]},
            {"timeout_s": 0},
            {"timeout_s": 500},
            {"privileged": True},
            {"image": "evil:latest"},
            {"HostConfig": {"Privileged": True}},
        ],
    )
    async def test_it_is_refused_before_anything_is_made(self, changes: dict[str, Any]) -> None:
        docker = FakeDocker()

        async with client_of(app_of(docker)) as http:
            response = await http.post("/v1/jobs", json=job_body(**changes))

        assert (response.status_code, error_code(response)) == (422, "invalid_request_body")
        assert docker.containers == {}
        assert docker.calls == []

    async def test_a_secret_that_was_refused_is_not_said_back(self) -> None:
        docker = FakeDocker()

        async with client_of(app_of(docker)) as http:
            response = await http.post("/v1/jobs", json=job_body(env={"PATH": "s3cret-value"}))

        assert "s3cret-value" not in response.text

    async def test_a_body_bigger_than_a_folder_can_be_is_refused_before_it_is_read(self) -> None:
        async with client_of(app_of(FakeDocker())) as http:
            response = await http.post(
                "/v1/jobs", content=b"x" * (MAX_BODY_BYTES + 1), headers={"content-type": "x"}
            )

        assert (response.status_code, error_code(response)) == (413, "payload_too_large")


class TestWhoMayAsk:
    async def test_a_caller_that_is_not_known_is_refused(self) -> None:
        docker = FakeDocker()

        async with client_of(app_of(docker)) as http:
            response = await http.post(
                "/v1/jobs", json=job_body(), headers={"x-factotum-dev-subject": "somebody"}
            )

        assert (response.status_code, error_code(response)) == (401, "auth_error")
        assert docker.containers == {}


class TestLiveness:
    async def test_it_is_alive_without_credentials(self) -> None:
        async with client_of(app_of(FakeDocker())) as http:
            response = await http.get("/healthz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    async def test_it_is_ready_when_jobs_can_be_run(self) -> None:
        async with client_of(app_of(FakeDocker())) as http:
            response = await http.get("/readyz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    @pytest.mark.parametrize("what", ["down", "no image"])
    async def test_it_is_not_ready_when_they_cannot(self, what: str) -> None:
        docker = FakeDocker()
        docker.down = what == "down"
        docker.image_present = what != "no image"

        async with client_of(app_of(docker)) as http:
            response = await http.get("/readyz")

        assert (response.status_code, response.json()) == (503, {"status": "unavailable"})

    @pytest.mark.parametrize(("docs", "status"), [(False, 404), (True, 200)])
    async def test_the_documentation_is_there_only_when_it_is_asked_for(
        self, docs: bool, status: int
    ) -> None:
        async with client_of(app_of(FakeDocker(), docs=docs)) as http:
            assert (await http.get("/docs")).status_code == status
