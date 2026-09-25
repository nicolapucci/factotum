"""The program that runs a plan, in the real sandbox: that what the actions service sends the runner
is something a container can run, and that what it does there is what it does anywhere.

The tests of the executor run it as a subprocess of the tests, which reaches whatever the machine
does. Here it is in a container as a run is made: the image the scripts run in, the user that
nothing else is, no network but what was granted (and nothing is, here). They need the socket of an
engine and the image (which is never pulled here) and skip without either: on Windows the engine is
not on a socket, so they run in a Linux container that is given one (docs/runner.md).
"""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from factotum.actions.instructions.testing import plan_dict, step_command, step_http
from factotum.actions.runner_client import EXECUTOR_ENTRYPOINT, executor_files, read_output
from factotum.contracts.commands import SetVolume, Speak
from factotum.contracts.errors import SkillRunError
from factotum.contracts.runner import Job, JobFile, JobResult
from factotum.runner.docker import DockerEngine, DockerError
from factotum.runner.sandbox import Sandbox, SandboxSettings

SOCKET = Path(os.environ.get("FACTOTUM_TEST_DOCKER_SOCKET", "/var/run/docker.sock"))
IMAGE = os.environ.get("FACTOTUM_TEST_SANDBOX_IMAGE", "python:3.13-slim")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.enable_socket,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(not SOCKET.exists(), reason=f"no engine on {SOCKET}"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine() -> AsyncIterator[DockerEngine]:
    client = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(SOCKET)),
        base_url="http://docker",
        timeout=httpx.Timeout(60.0),
    )
    found = DockerEngine(client)
    try:
        await found.ping()
        if not await found.has_image(IMAGE):
            pytest.skip(f"the image {IMAGE} is not on this engine (it is not pulled here)")
    except DockerError as error:  # pragma: no cover - there is a socket and nothing on it
        pytest.skip(str(error))
    yield found
    await client.aclose()


async def run_plan(
    engine: DockerEngine,
    plan: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    device_params: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> JobResult:
    """The job the actions service makes for a plan, and what the sandbox makes of it."""
    given = {
        "plan": plan,
        "params": params or {},
        "device_params": device_params or {},
        "secrets": sorted(secrets or {}),
        "budget_s": 10,
    }
    job = Job(
        label="plan-test",
        files=tuple(JobFile.of(path, data) for path, data in executor_files().items()),
        entrypoint=EXECUTOR_ENTRYPOINT,
        stdin=json.dumps(given),
        env=secrets or {},
        timeout_s=20,
    )
    sandbox = Sandbox(
        engine, SandboxSettings(image=IMAGE, label="factotum.sandbox.test", grace_s=15)
    )
    return await sandbox.run(job)


class TestAPlanInTheSandbox:
    async def test_a_plan_of_commands_is_worked_out_and_answered_as_a_script_answers(
        self, engine: DockerEngine
    ) -> None:
        plan = plan_dict(
            step_command("say", "speak", text="${value.message}"),
            step_command("loud", "set_volume", level="${value.level}"),
            say="Told ${device.name}: ${value.message}",
        )

        result = await run_plan(
            engine,
            plan,
            params={"message": "la cena è pronta", "level": 60},
            device_params={"name": "the kitchen"},
        )

        assert result.exit_code == 0, result.stderr
        answer = read_output("plan-test", result)
        assert answer.message == "Told the kitchen: la cena è pronta"
        assert list(answer.commands) == [Speak(text="la cena è pronta"), SetVolume(level=60)]

    async def test_nothing_of_the_machine_is_there_to_reach_when_no_host_was_granted(
        self, engine: DockerEngine
    ) -> None:
        plan = plan_dict(step_http("look", "GET", "http://192.0.2.1/ip"))

        result = await run_plan(engine, plan)

        assert result.exit_code == 1
        assert "look: the request did not get through" in result.stderr
        with pytest.raises(SkillRunError, match="exit code 1"):
            read_output("plan-test", result)

    async def test_a_secret_is_in_the_environment_of_the_run_and_never_in_what_it_says(
        self, engine: DockerEngine
    ) -> None:
        plan = plan_dict(step_command("leak", "speak", text="${secret.HA_TOKEN}"))

        result = await run_plan(engine, plan, secrets={"HA_TOKEN": "the-token-of-the-home"})

        assert result.exit_code == 1
        assert "a secret is not for saying" in result.stderr
        assert "the-token-of-the-home" not in result.stdout + result.stderr

    async def test_what_is_not_a_plan_is_refused_with_its_own_exit_code(
        self, engine: DockerEngine
    ) -> None:
        job = Job(
            label="plan-test",
            files=tuple(JobFile.of(path, data) for path, data in executor_files().items()),
            entrypoint=EXECUTOR_ENTRYPOINT,
            stdin="not json",
            timeout_s=20,
        )

        result = await Sandbox(
            engine, SandboxSettings(image=IMAGE, label="factotum.sandbox.test", grace_s=15)
        ).run(job)

        assert result.exit_code == 2
        assert result.stdout == ""
