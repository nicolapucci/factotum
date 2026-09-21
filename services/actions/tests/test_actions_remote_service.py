"""The action path with the runner and the edge behind it, made of the real clients of the two
services (not of doubles of ``SkillRunner`` and ``CommandDispatcher``), talking to doubles of the
services: what the person is told, what the trail says, and what goes over the wire when a message
asks for a lamp, a volume or a door."""

import json
from typing import Any

import httpx

from factotum.actions.edge_client import EdgeDispatcher
from factotum.actions.rig import TEST_SETTINGS, ActionsRig, plan_of
from factotum.actions.runner_client import RemoteRunner
from factotum.actions.secrets import FileSecrets, parse_secrets
from factotum.actions.testing import LAMP, VOLUME, WEATHER
from factotum.common.devauth import DevPrincipalDelegation
from factotum.contracts.actions import ActCode, ActStatus
from factotum.contracts.audit import ActionResult
from factotum.contracts.commands import CommandStatus, SetVolume
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.runner import Job, JobResult

KITCHEN = "Kitchen speaker"
TOKEN = "the-token-of-the-home"
SECRETS = f"""\
tenant:
  HA_TOKEN: "{TOKEN}"
"""


def printed(**fields: Any) -> httpx.Response:
    return httpx.Response(
        200, json=json.loads(JobResult(exit_code=0, stdout=json.dumps(fields)).model_dump_json())
    )


class Services:
    """The runner and the edge, as far as the actions service can tell."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self.commands: list[httpx.Request] = []
        self.run: Any = lambda job: printed(message="Done.")
        self.edge: Any = lambda request: httpx.Response(200, json={"status": "ok"})

    def runner_side(self, request: httpx.Request) -> httpx.Response:
        job = Job.model_validate_json(request.content)
        self.jobs.append(job)
        response: httpx.Response = self.run(job)
        return response

    def edge_side(self, request: httpx.Request) -> httpx.Response:
        self.commands.append(request)
        response: httpx.Response = self.edge(request)
        return response


def wire(rig: ActionsRig, services: Services, secrets: str = SECRETS) -> None:
    """Have the rig's service run skills in the runner and give commands through the edge."""
    delegation = DevPrincipalDelegation(TEST_SETTINGS)
    held = FileSecrets(
        TEST_SETTINGS,
        parse_secrets(secrets, tenant_id=rig.tenant, user_id=rig.user),
        tenant_id=rig.tenant,
    )
    runner = RemoteRunner(
        httpx.AsyncClient(
            transport=httpx.MockTransport(services.runner_side), base_url="http://runner.test"
        ),
        store=rig.store,
        secrets=held,
        delegation=delegation,
    )
    edge = EdgeDispatcher(
        httpx.AsyncClient(
            transport=httpx.MockTransport(services.edge_side), base_url="http://edge.test"
        ),
        delegation,
    )
    rig.use_runner(runner, edge)


class TestALampThroughTheRunner:
    async def test_the_lamp_is_switched_by_a_script_that_is_given_what_it_needs(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        await rig.install(LAMP)
        services.run = lambda job: printed(message="The lamp is on.")
        rig.plans(
            plan_of("lamp", device="Living room lamp", parameters={"state": "on", "brightness": 40})
        )

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp to 40")

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "The lamp is on.")
        [job] = services.jobs
        assert job.entrypoint == "scripts/run.py"
        assert json.loads(job.stdin) == {
            "params": {"state": "on", "brightness": 40},
            "device_params": {"entity_id": "light.living_room", "host": "homeassistant.local"},
        }
        assert job.env == {"HA_TOKEN": TOKEN}
        assert job.hosts == ("homeassistant.local",)
        [record] = await rig.audit()
        assert record.result is ActionResult.EXECUTED

    async def test_the_person_is_never_told_the_secret_and_the_trail_never_holds_it(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert TOKEN not in outcome.model_dump_json()
        assert all(TOKEN not in record.model_dump_json() for record in await rig.audit())

    async def test_a_lamp_that_is_a_personal_one_is_not_given_the_secrets_of_the_home(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        mine = Owner(kind=OwnerKind.USER, id=rig.user)
        await rig.install(LAMP, owner=mine)
        rig.plans(plan_of("lamp", device="Bedroom lamp", parameters={"state": "on"}))

        outcome = await rig.hear("My phone", "turn on my lamp")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
        assert services.jobs == []
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED

    async def test_a_script_that_fails_is_a_skill_that_failed(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        await rig.install(LAMP)
        services.run = lambda job: httpx.Response(
            200, json={"exit_code": 1, "stderr": "KeyError: 'entity_id'"}
        )
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
        assert outcome.reply == "It did not work."
        [record] = await rig.audit()
        assert (record.result, record.detail) == (
            ActionResult.FAILED,
            "the skill failed: SkillRunError",
        )

    async def test_a_script_that_takes_too_long_is_a_skill_that_failed(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        await rig.install(LAMP)
        services.run = lambda job: httpx.Response(200, json={"timed_out": True})
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert outcome.code is ActCode.SKILL_FAILED

    async def test_a_runner_that_is_not_there_is_said_to_be_so(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        await rig.install(LAMP)

        def refuse(job: Job) -> httpx.Response:
            raise httpx.ConnectError("refused")

        services.run = refuse
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))

        outcome = await rig.hear(KITCHEN, "turn on the living room lamp")

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.RUNNER_UNAVAILABLE)
        [record] = await rig.audit()
        assert (record.result, record.detail) == (ActionResult.FAILED, "nothing can run skills")


class TestACommandThroughTheEdge:
    async def volume(self, rig: ActionsRig, services: Services, *, device: str = KITCHEN) -> Any:
        await rig.install(VOLUME)
        services.run = lambda job: printed(
            message="Volume set to 30.", commands=[{"name": "set_volume", "level": 30}]
        )
        rig.plans(plan_of("volume", parameters={"level": 30}))
        return await rig.hear(device, "set the volume to 30")

    async def test_what_the_script_answers_with_reaches_the_device_that_was_spoken_to(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)

        outcome = await self.volume(rig, services)

        assert (outcome.status, outcome.reply) == (ActStatus.DONE, "Volume set to 30.")
        [sent] = services.commands
        assert sent.url.path == f"/v1/devices/{rig.device(KITCHEN).id}/commands"
        assert json.loads(sent.content) == {"command": {"name": "set_volume", "level": 30}}
        assert sent.headers["x-factotum-dev-subject"] == "tenant"
        [record] = await rig.audit()
        assert record.result is ActionResult.EXECUTED

    async def test_a_script_that_needs_no_network_is_given_none(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)

        await self.volume(rig, services)

        [job] = services.jobs
        assert (job.hosts, job.env) == ((), {})
        assert json.loads(job.stdin) == {"params": {"level": 30}, "device_params": {}}

    async def test_a_device_that_is_not_connected_makes_the_action_fail_and_says_so(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        services.edge = lambda request: httpx.Response(
            200, json={"status": CommandStatus.OFFLINE.value, "detail": "not connected"}
        )

        outcome = await self.volume(rig, services)

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
        [record] = await rig.audit()
        assert record.result is ActionResult.FAILED

    async def test_an_edge_that_cannot_be_reached_makes_the_action_fail_and_says_so(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        services.edge = refuse

        outcome = await self.volume(rig, services)

        assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)

    async def test_a_command_is_only_given_after_the_checks_and_the_device_that_was_meant(
        self,
    ) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        await rig.install(VOLUME)
        services.run = lambda job: printed(
            message="Louder.", commands=[{"name": "set_volume", "level": 90}]
        )
        rig.plans(plan_of("volume", device="the player", parameters={"level": 90}))

        await rig.hear(KITCHEN, "set the volume to 90 on the player")

        [sent] = services.commands
        assert sent.url.path == f"/v1/devices/{rig.device('Study player').id}/commands"
        assert SetVolume(level=90).model_dump() == json.loads(sent.content)["command"]


class TestAWeatherThatNeedsTheWorld:
    async def test_a_skill_that_reads_from_the_world_is_given_the_hosts_and_no_secret(self) -> None:
        rig, services = ActionsRig(), Services()
        wire(rig, services)
        await rig.install(WEATHER)
        services.run = lambda job: printed(message="In Rome it is 21 degrees.")
        rig.plans(plan_of("weather", parameters={"place": "Rome"}))

        outcome = await rig.hear(KITCHEN, "what is the weather like in Rome")

        assert outcome.reply == "In Rome it is 21 degrees."
        [job] = services.jobs
        assert job.hosts == ("api.open-meteo.com",)
        assert job.env == {}
        assert services.commands == []
