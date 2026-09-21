"""Running a skill in the runner: what the runner is asked, and what is made of its answer. The
runner itself is a double that records what it is sent, so what is checked is this side of the wire:
that the files come from the store, the secrets from their owner, the hosts from the grant, and that
what a script prints is believed no further than it can be checked."""

import json
import logging
from typing import Any
from uuid import uuid4

import httpx
import pytest

from factotum.actions.runner_client import RemoteRunner, hosts_of, read_output
from factotum.actions.secrets import FileSecrets, NoSecrets, SecretResolver, parse_secrets
from factotum.actions.skills import LoadedSkill
from factotum.actions.store import ActionsStore, InMemoryStore
from factotum.actions.testing import LAMP, START, VOLUME, WEATHER
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevPrincipalDelegation
from factotum.contracts.commands import SetVolume
from factotum.contracts.errors import RunnerUnavailableError, SkillRunError
from factotum.contracts.identity import Owner, OwnerKind, Subject
from factotum.contracts.ids import SkillId
from factotum.contracts.runner import Job, JobResult, RunRequest
from factotum.contracts.skills import ParamValue, SkillManifest, SkillMode, SkillPermissions
from factotum.contracts.testing import make_tenant_id, make_user_id

TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)
TENANT = make_tenant_id()
USER = make_user_id()
OF_THE_TENANT = Owner.of_subject(Subject.tenant(TENANT))
OF_THE_USER = Owner.of_subject(Subject.user(USER))
TOKEN = "the-token-of-the-home"
OWN_TOKEN = "my-own-token"
SECRETS = f"""\
tenant:
  HA_TOKEN: "{TOKEN}"
user:
  HA_TOKEN: "{OWN_TOKEN}"
"""
FILES = {"scripts/run.py": b"print('{}')\n", "scripts/helper.py": b"X = 1\n"}
DEVICE = {"entity_id": "light.living_room", "host": "Home-Assistant.local"}
SPOKEN: dict[str, ParamValue] = {"state": "on", "brightness": 40}


def answer(**fields: Any) -> str:
    return JobResult(exit_code=0, stdout=json.dumps(fields), duration_ms=120).model_dump_json()


class Runner:
    """A runner that records what it is sent and answers with what it is told to."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.reply: Any = lambda request: httpx.Response(200, text=answer(message="Done."))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        response: httpx.Response = self.reply(request)
        return response

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://runner.test")

    @property
    def job(self) -> Job:
        assert len(self.sent) == 1
        return Job.model_validate_json(self.sent[0].content)


async def install(
    store: ActionsStore,
    manifest: SkillManifest,
    *,
    owner: Owner = OF_THE_TENANT,
    files: dict[str, bytes] | None = None,
) -> SkillId:
    skill = LoadedSkill(
        manifest=manifest,
        instructions="",
        source=f"---\nname: {manifest.name}\n---\n",
        files=FILES if files is None else files,
    )
    installed = await store.install(TENANT, owner, skill, now=START)
    await store.enable(
        TENANT,
        installed.skill_id,
        installed.version,
        granted=manifest.permissions,
        by=None,
        now=START,
    )
    return installed.skill_id


def request_for(
    skill_id: SkillId,
    manifest: SkillManifest = LAMP,
    *,
    owner: Owner = OF_THE_TENANT,
    permissions: SkillPermissions | None = None,
    **changes: Any,
) -> RunRequest:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "owner": owner,
        "skill_id": skill_id,
        "skill_name": manifest.name,
        "version": 1,
        "mode": manifest.mode,
        "entrypoint": manifest.entrypoint,
        "params": SPOKEN,
        "device_params": DEVICE,
        "permissions": manifest.permissions if permissions is None else permissions,
        "timeout_s": manifest.timeout_s,
    }
    return RunRequest.model_validate({**fields, **changes})


def a_runner(
    runner: Runner,
    store: ActionsStore,
    secrets: SecretResolver | None = None,
    *,
    margin_s: float = 10.0,
) -> RemoteRunner:
    held = secrets or FileSecrets(
        TEST, parse_secrets(SECRETS, tenant_id=TENANT, user_id=USER), tenant_id=TENANT
    )
    return RemoteRunner(
        runner.client(),
        store=store,
        secrets=held,
        delegation=DevPrincipalDelegation(TEST),
        margin_s=margin_s,
    )


class TestWhatTheRunnerIsAsked:
    async def test_it_runs_the_files_of_the_version_that_was_enabled(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        await a_runner(runner, store).run(request_for(skill_id))

        job = runner.job
        assert {file.path: file.data for file in job.files} == FILES
        assert (job.entrypoint, job.label, job.timeout_s) == ("scripts/run.py", "lamp", 10)
        assert runner.sent[0].method == "POST"
        assert runner.sent[0].url.path == "/v1/jobs"

    async def test_the_files_are_those_of_the_version_that_is_asked_for(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP, files={"scripts/run.py": b"print(1)\n"})
        newer = LoadedSkill(
            manifest=LAMP,
            instructions="",
            source="---\nname: lamp\n---\nchanged\n",
            files={"scripts/run.py": b"print(2)\n"},
        )
        await store.install(TENANT, OF_THE_TENANT, newer, now=START)

        await a_runner(runner, store).run(request_for(skill_id, version=1))

        assert [file.data for file in runner.job.files] == [b"print(1)\n"]

    async def test_the_script_reads_its_parameters_on_standard_input(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        await a_runner(runner, store).run(request_for(skill_id))

        assert json.loads(runner.job.stdin) == {"params": SPOKEN, "device_params": DEVICE}

    async def test_it_is_given_the_secrets_it_was_granted_by_their_names(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        await a_runner(runner, store).run(request_for(skill_id))

        assert runner.job.env == {"HA_TOKEN": TOKEN}

    async def test_it_is_given_no_secret_it_was_not_granted(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)
        nothing = request_for(skill_id, permissions=SkillPermissions(network=("${device.host}",)))

        await a_runner(runner, store).run(nothing)

        assert runner.job.env == {}

    async def test_it_may_reach_what_it_was_granted_and_the_host_of_its_device(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)
        granted = SkillPermissions(network=("${device.host}", "api.example.org"))

        await a_runner(runner, store).run(request_for(skill_id, permissions=granted))

        assert runner.job.hosts == ("home-assistant.local", "api.example.org")

    async def test_a_skill_that_was_granted_no_network_is_asked_to_have_none(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, VOLUME)

        await a_runner(runner, store).run(
            request_for(skill_id, VOLUME, params={"level": 30}, device_params={})
        )

        assert runner.job.hosts == ()

    async def test_it_acts_for_the_tenant(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP, owner=OF_THE_USER)

        await a_runner(runner, store).run(request_for(skill_id, owner=OF_THE_USER))

        sent = runner.sent[0]
        assert sent.headers["x-factotum-dev-subject"] == "tenant"
        assert sent.headers["content-type"] == "application/json"

    async def test_the_runner_is_waited_for_a_little_longer_than_the_script(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        await a_runner(runner, store, margin_s=7.0).run(request_for(skill_id, timeout_s=20))

        timeout = runner.sent[0].extensions["timeout"]
        assert timeout["read"] == 27.0
        assert timeout["connect"] == 3.0


class TestWhoseSecretsTheyAre:
    async def test_a_skill_of_a_person_is_given_the_secrets_of_that_person(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP, owner=OF_THE_USER)

        await a_runner(runner, store).run(request_for(skill_id, owner=OF_THE_USER))

        assert runner.job.env == {"HA_TOKEN": OWN_TOKEN}

    async def test_a_skill_of_somebody_who_has_no_such_secret_does_not_run(self) -> None:
        store, runner = InMemoryStore(), Runner()
        stranger = Owner(kind=OwnerKind.USER, id=uuid4())
        skill_id = await install(store, LAMP, owner=stranger)

        with pytest.raises(SkillRunError, match="HA_TOKEN is not set") as raised:
            await a_runner(runner, store).run(request_for(skill_id, owner=stranger))

        assert runner.sent == []
        assert TOKEN not in str(raised.value)
        assert OWN_TOKEN not in str(raised.value)

    async def test_a_skill_that_needs_a_secret_where_none_are_kept_does_not_run(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        with pytest.raises(SkillRunError, match="HA_TOKEN is not set"):
            await a_runner(runner, store, NoSecrets()).run(request_for(skill_id))

        assert runner.sent == []


class TestWhatIsNotRun:
    async def test_a_skill_without_a_script_is_not_run_by_the_runner(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        with pytest.raises(SkillRunError, match="no script"):
            await a_runner(runner, store).run(
                request_for(skill_id, mode=SkillMode.INSTRUCTIONS, entrypoint=None)
            )

        assert runner.sent == []

    async def test_a_skill_that_needs_a_linked_account_says_so_and_is_not_run(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)
        needs = SkillPermissions.model_validate(
            {"accounts": [{"provider": "spotify", "scope": "user"}]}
        )

        with pytest.raises(SkillRunError, match="linked account"):
            await a_runner(runner, store).run(request_for(skill_id, permissions=needs))

        assert runner.sent == []

    async def test_a_version_whose_files_are_gone_is_not_run(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        with pytest.raises(SkillRunError, match="are not there"):
            await a_runner(runner, store).run(request_for(skill_id, version=7))

        assert runner.sent == []

    async def test_a_script_that_is_not_among_the_files_is_not_run(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP, files={"scripts/other.py": b"x"})

        with pytest.raises(SkillRunError, match="are not there"):
            await a_runner(runner, store).run(request_for(skill_id))

        assert runner.sent == []

    async def test_a_skill_that_belongs_to_another_tenant_is_not_run(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)
        other = make_tenant_id()

        with pytest.raises(SkillRunError, match="are not there"):
            await a_runner(runner, store).run(request_for(skill_id, tenant_id=other))

        assert runner.sent == []

    async def test_a_device_that_has_no_host_gives_nothing_to_reach(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        with pytest.raises(SkillRunError, match="no host that is a host to reach"):
            await a_runner(runner, store).run(request_for(skill_id, device_params={}))

        assert runner.sent == []

    async def test_what_the_runner_would_refuse_is_not_sent_and_says_what_not_who(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)
        too_much = "x" * 70_000

        with pytest.raises(SkillRunError, match="cannot be given to the runner") as raised:
            await a_runner(runner, store).run(request_for(skill_id, params={"state": too_much}))

        assert runner.sent == []
        assert too_much not in str(raised.value)


class TestWhereItMayGo:
    def test_a_host_of_the_grant_is_a_host_of_the_run(self) -> None:
        permissions = SkillPermissions(network=("api.open-meteo.com", "*.example.org"))

        assert hosts_of(permissions, {}) == ("api.open-meteo.com", "*.example.org")

    def test_a_reference_is_the_value_the_device_was_bound_with(self) -> None:
        permissions = SkillPermissions(network=("${device.host}",))

        assert hosts_of(permissions, {"host": "192.168.1.20"}) == ("192.168.1.20",)

    def test_hosts_are_lower_case_and_listed_once(self) -> None:
        permissions = SkillPermissions(network=("${device.host}", "HA.local", "ha.LOCAL"))

        assert hosts_of(permissions, {"host": "Ha.Local"}) == ("ha.local",)

    @pytest.mark.parametrize(
        "value",
        [
            "*.evil.example",
            "*",
            "",
            "a b",
            "http://x.example",
            "x.example/path",
            "x:8123",
            8123,
            True,
        ],
        ids=lambda value: repr(value)[:14],
    )
    def test_what_a_device_is_bound_with_is_one_host_and_never_a_pattern(
        self, value: ParamValue
    ) -> None:
        permissions = SkillPermissions(network=("${device.host}",))

        with pytest.raises(SkillRunError, match="host to reach"):
            hosts_of(permissions, {"host": value})

    def test_a_device_that_lacks_the_value_is_refused(self) -> None:
        with pytest.raises(SkillRunError, match="no host that is a host"):
            hosts_of(SkillPermissions(network=("${device.host}",)), {"entity_id": "x"})

    def test_a_grant_without_a_network_reaches_nothing(self) -> None:
        assert hosts_of(SkillPermissions(), {"host": "ha.local"}) == ()


class TestWhatWasPrinted:
    def read(self, **fields: Any) -> Any:
        return read_output("lamp", JobResult(exit_code=0, stdout=json.dumps(fields)))

    def test_a_message_what_was_found_and_what_is_to_be_done(self) -> None:
        result = self.read(
            message="Volume set to 40.",
            data={"level": 40},
            commands=[{"name": "set_volume", "level": 40}],
        )

        assert result.message == "Volume set to 40."
        assert result.data == {"level": 40}
        assert result.commands == (SetVolume(level=40),)
        assert result.dry_run is False

    def test_a_script_that_prints_nothing_has_nothing_to_say(self) -> None:
        result = read_output("lamp", JobResult(exit_code=0, stdout=" \n"))

        assert (result.message, result.data, result.commands) == (None, {}, ())

    def test_what_is_around_the_document_does_not_matter(self) -> None:
        result = read_output("lamp", JobResult(exit_code=0, stdout='\n  {"message": "Hi."}  \n\n'))

        assert result.message == "Hi."

    def test_a_script_cannot_say_that_nothing_was_run(self) -> None:
        with pytest.raises(SkillRunError, match="dry_run"):
            self.read(message="Done.", dry_run=True)

    @pytest.mark.parametrize(
        ("result", "problem"),
        [
            (JobResult(timed_out=True), "did not finish in time"),
            (JobResult(exit_code=0, oom_killed=True), "more memory"),
            (JobResult(exit_code=1), "exit code 1"),
            (JobResult(exit_code=137, stderr="Killed"), "exit code 137"),
            (JobResult(exit_code=None), "exit code None"),
            (JobResult(exit_code=0, stdout="not json"), "one JSON document"),
            (JobResult(exit_code=0, stdout='{"message": "a"} {"message": "b"}'), "one JSON"),
            (JobResult(exit_code=0, stdout='{"message": "cut'), "one JSON document"),
            (JobResult(exit_code=0, stdout="[1, 2]"), "not one a skill gives"),
            (JobResult(exit_code=0, stdout='"text"'), "not one a skill gives"),
            (JobResult(exit_code=0, stdout='{"messsage": "typo"}'), "messsage"),
            (JobResult(exit_code=0, stdout='{"message": ""}'), "message"),
            (JobResult(exit_code=0, stdout='{"commands": [{"name": "explode"}]}'), "commands"),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )
    def test_whatever_is_not_a_good_ending_is_a_failure_of_the_skill(
        self, result: JobResult, problem: str
    ) -> None:
        with pytest.raises(SkillRunError, match=problem):
            read_output("lamp", result)

    def test_it_says_what_is_wrong_and_never_what_the_script_said(self) -> None:
        printed = json.dumps({"messsage": "s3cr3t-value", "data": {"key": "s3cr3t-value"}})

        with pytest.raises(SkillRunError) as raised:
            read_output("lamp", JobResult(exit_code=0, stdout=printed))

        assert "s3cr3t-value" not in str(raised.value)
        assert "messsage" in str(raised.value)

    def test_a_script_that_failed_leaves_the_end_of_what_it_wrote_in_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="factotum.actions.runner")
        stderr = "x" * 1000 + "KeyError: 'entity_id'"

        with pytest.raises(SkillRunError):
            read_output("lamp", JobResult(exit_code=1, stderr=stderr))

        record = next(record for record in caplog.records if record.message == "a script failed")
        assert record.skill == "lamp"  # type: ignore[attr-defined]
        assert record.stderr.endswith("KeyError: 'entity_id'")  # type: ignore[attr-defined]
        assert len(record.stderr) == 300  # type: ignore[attr-defined]


class TestWhenTheRunnerFails:
    @pytest.fixture
    def parts(self) -> tuple[InMemoryStore, Runner]:
        return InMemoryStore(), Runner()

    @pytest.mark.parametrize(
        "raised",
        [
            httpx.ConnectError("refused"),
            httpx.ConnectTimeout("slow"),
            httpx.ReadTimeout("slow"),
            httpx.RemoteProtocolError("cut"),
        ],
        ids=lambda error: type(error).__name__,
    )
    async def test_a_runner_that_cannot_be_reached_is_one_that_is_not_there(
        self, parts: tuple[InMemoryStore, Runner], raised: httpx.HTTPError
    ) -> None:
        store, runner = parts
        skill_id = await install(store, LAMP)

        def refuse(request: httpx.Request) -> httpx.Response:
            raise raised

        runner.reply = refuse

        with pytest.raises(RunnerUnavailableError, match="could not be reached"):
            await a_runner(runner, store).run(request_for(skill_id))

    @pytest.mark.parametrize("status", [401, 403, 429, 502, 503, 504])
    async def test_a_runner_that_says_it_cannot_be_used_is_one_that_is_not_there(
        self, parts: tuple[InMemoryStore, Runner], status: int
    ) -> None:
        store, runner = parts
        skill_id = await install(store, LAMP)
        runner.reply = lambda request: httpx.Response(status, json={"error": {"code": "x"}})

        with pytest.raises(RunnerUnavailableError, match=str(status)):
            await a_runner(runner, store).run(request_for(skill_id))

    @pytest.mark.parametrize("status", [400, 404, 413, 422, 500])
    async def test_a_runner_that_refuses_the_job_has_failed_the_skill(
        self, parts: tuple[InMemoryStore, Runner], status: int
    ) -> None:
        store, runner = parts
        skill_id = await install(store, LAMP)
        runner.reply = lambda request: httpx.Response(status, json={"error": {"code": "x"}})

        with pytest.raises(SkillRunError, match=str(status)) as raised:
            await a_runner(runner, store).run(request_for(skill_id))

        assert not isinstance(raised.value, RunnerUnavailableError)

    @pytest.mark.parametrize("body", ["not json", "[]", '{"exit_code": "many"}', '{"stdout": 1}'])
    async def test_an_answer_that_is_not_the_result_of_a_job_is_a_runner_that_is_not_right(
        self, parts: tuple[InMemoryStore, Runner], body: str
    ) -> None:
        store, runner = parts
        skill_id = await install(store, LAMP)
        runner.reply = lambda request: httpx.Response(200, text=body)

        with pytest.raises(RunnerUnavailableError, match="not the result of a job"):
            await a_runner(runner, store).run(request_for(skill_id))

    async def test_a_script_that_failed_is_an_answer_that_fails_the_skill(
        self, parts: tuple[InMemoryStore, Runner]
    ) -> None:
        store, runner = parts
        skill_id = await install(store, LAMP)
        failed = JobResult(exit_code=1, stderr="Traceback").model_dump_json()
        runner.reply = lambda request: httpx.Response(200, text=failed)

        with pytest.raises(SkillRunError, match="exit code 1") as raised:
            await a_runner(runner, store).run(request_for(skill_id))

        assert not isinstance(raised.value, RunnerUnavailableError)


class TestWhatComesOutOfARun:
    async def test_what_the_script_printed_is_what_the_skill_answers_with(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, VOLUME)
        runner.reply = lambda request: httpx.Response(
            200,
            text=answer(
                message="Volume set to 30.", commands=[{"name": "set_volume", "level": 30}]
            ),
        )

        result = await a_runner(runner, store).run(
            request_for(skill_id, VOLUME, params={"level": 30}, device_params={})
        )

        assert result.message == "Volume set to 30."
        assert result.commands == (SetVolume(level=30),)
        assert not result.dry_run

    async def test_a_run_leaves_a_line_in_the_log_that_says_no_more_than_how_it_went(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="factotum.actions.runner")
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, LAMP)

        await a_runner(runner, store).run(request_for(skill_id))

        record = next(record for record in caplog.records if record.message == "a skill was run")
        assert (record.skill, record.version, record.exit_code, record.duration_ms) == (  # type: ignore[attr-defined]
            "lamp",
            1,
            0,
            120,
        )
        assert TOKEN not in caplog.text

    async def test_the_weather_is_asked_for_without_a_device_or_a_secret(self) -> None:
        store, runner = InMemoryStore(), Runner()
        skill_id = await install(store, WEATHER)

        await a_runner(runner, store).run(
            request_for(skill_id, WEATHER, params={"place": "Rome"}, device_params={})
        )

        job = runner.job
        assert job.hosts == ("api.open-meteo.com",)
        assert job.env == {}
