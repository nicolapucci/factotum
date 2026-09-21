"""A container for each run of a script: what it is like, what it is given, what becomes of it."""

import asyncio
import itertools
from typing import Any

import pytest

from factotum.common import egress
from factotum.contracts.runner import Job, JobFile
from factotum.runner.sandbox import (
    FIXED_ENV,
    Sandbox,
    SandboxSettings,
    SandboxUnavailable,
    archive_of,
    container_body,
    proxy_url,
)
from factotum.runner.testing import FakeDocker, Fate

KEY = b"r" * 32
NOW = 1_800_000_000


def job(**changes: Any) -> Job:
    fields: dict[str, Any] = {
        "label": "lamp",
        "files": (JobFile.of("scripts/run.py", b"print('{}')"),),
        "entrypoint": "scripts/run.py",
        "stdin": '{"params": {"state": "on"}}',
        "timeout_s": 5,
    }
    return Job.model_validate({**fields, **changes})


def settings(**changes: Any) -> SandboxSettings:
    fields: dict[str, Any] = {"egress_key": KEY, "grace_s": 1.0, "queue_wait_s": 0.2}
    return SandboxSettings(**{**fields, **changes})


def body_of(made: Job, **changes: Any) -> dict[str, Any]:
    body, _ = container_body(made, "run1", settings(**changes), expires_at=NOW + 30)
    return body


def env_of(body: dict[str, Any]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in body["Env"])


def sandbox_of(docker: FakeDocker, **changes: Any) -> Sandbox:
    counter = itertools.count(1)
    return Sandbox(
        docker.engine(), settings(**changes), now=lambda: NOW, new_run=lambda: f"run{next(counter)}"
    )


class TestWhatAContainerIsLike:
    def test_it_runs_as_somebody_who_owns_nothing_with_no_capability_and_no_way_to_get_one(
        self,
    ) -> None:
        body = body_of(job())

        assert body["User"] == "10002:10002"
        assert body["HostConfig"]["CapDrop"] == ["ALL"]
        assert body["HostConfig"]["SecurityOpt"] == ["no-new-privileges"]
        assert body["HostConfig"]["Privileged"] is False
        assert "CapAdd" not in body["HostConfig"]

    def test_it_is_given_a_limit_on_what_it_may_use(self) -> None:
        host = body_of(job(), memory_mb=64, cpus=0.25, pids=16)["HostConfig"]

        assert host["Memory"] == host["MemorySwap"] == 64 * 1024 * 1024
        assert host["NanoCpus"] == 250_000_000
        assert host["PidsLimit"] == 16
        assert {"Name": "core", "Soft": 0, "Hard": 0} in host["Ulimits"]

    def test_it_may_write_only_to_a_small_memory_of_its_own_that_runs_nothing(self) -> None:
        host = body_of(job(), tmp_mb=8)["HostConfig"]

        assert host["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,nodev,size=8m"}  # noqa: S108

    def test_nothing_of_the_machine_is_in_it(self) -> None:
        body = body_of(job(hosts=["a.org"]))
        host = body["HostConfig"]

        # Whoever adds to what a container is given must say so here: this is the list.
        assert set(host) == {
            "NetworkMode",
            "Privileged",
            "PublishAllPorts",
            "CapDrop",
            "SecurityOpt",
            "Memory",
            "MemorySwap",
            "NanoCpus",
            "PidsLimit",
            "Tmpfs",
            "IpcMode",
            "AutoRemove",
            "Ulimits",
            "LogConfig",
        }
        assert host["IpcMode"] == "none"
        for key in (
            "Binds",
            "Mounts",
            "Devices",
            "PortBindings",
            "Links",
            "VolumesFrom",
            "PidMode",
        ):
            assert key not in host
        assert "Volumes" not in body
        assert "ExposedPorts" not in body

    def test_the_engine_keeps_only_a_little_of_what_it_writes(self) -> None:
        log = body_of(job(), log_kb=64)["HostConfig"]["LogConfig"]

        assert log == {"Type": "json-file", "Config": {"max-size": "64k", "max-file": "1"}}

    def test_the_script_is_started_by_the_program_that_gives_it_its_input_and_not_by_a_shell(
        self,
    ) -> None:
        cmd = body_of(job())["Cmd"]

        assert cmd == ["python", "-E", "-s", "/opt/factotum/shim.py", "/skill/scripts/run.py"]

    def test_it_is_labelled_so_that_what_is_left_of_it_can_be_found(self) -> None:
        labels = body_of(job())["Labels"]

        assert labels == {"factotum.sandbox": "1", "factotum.run": "run1", "factotum.job": "lamp"}

    def test_it_is_made_from_the_image_it_is_told_and_nothing_a_job_says(self) -> None:
        assert body_of(job(), image="python:3.13-slim")["Image"] == "python:3.13-slim"
        assert body_of(job(), image="mine:1")["Image"] == "mine:1"

    def test_it_is_not_given_the_input_to_read_and_nothing_can_be_typed_to_it(self) -> None:
        body = body_of(job())

        assert not any(body[key] for key in ("AttachStdin", "OpenStdin", "Tty"))


class TestItsNetwork:
    def test_with_no_hosts_it_is_on_no_network_at_all(self) -> None:
        body = body_of(job())

        assert body["HostConfig"]["NetworkMode"] == "none"
        assert not [name for name in env_of(body) if "PROXY" in name.upper()]

    def test_with_hosts_it_is_on_the_network_that_has_only_the_proxy_and_is_given_its_grant(
        self,
    ) -> None:
        body, _ = container_body(
            job(hosts=["homeassistant.local", "*.example.org"]),
            "run7",
            settings(network="sandbox-net", egress_url="http://egress:3128"),
            expires_at=NOW + 30,
        )

        env = env_of(body)
        assert body["HostConfig"]["NetworkMode"] == "sandbox-net"
        assert {
            env[name] for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        } == {env["HTTP_PROXY"]}
        user, _, rest = env["HTTP_PROXY"].removeprefix("http://").partition(":")
        token, _, address = rest.partition("@")
        assert (user, address) == ("skill", "egress:3128")
        grant = egress.verify(KEY, token, now=NOW)
        assert grant == egress.Grant(
            run="run7", hosts=("homeassistant.local", "*.example.org"), expires_at=NOW + 30
        )

    def test_the_grant_stops_being_good_when_the_job_should_have_ended(self) -> None:
        body, _ = container_body(job(hosts=["a.org"]), "r", settings(), expires_at=NOW + 30)
        token = env_of(body)["HTTP_PROXY"].split(":", 2)[2].rsplit("@", 1)[0]

        egress.verify(KEY, token, now=NOW + 30)
        with pytest.raises(Exception, match="not a grant"):
            egress.verify(KEY, token, now=NOW + 31)

    @pytest.mark.parametrize(
        "changes",
        [{"egress_url": None}, {"egress_key": None}, {"egress_url": None, "egress_key": None}],
    )
    def test_with_hosts_but_no_proxy_to_go_through_it_is_on_no_network_either(
        self, changes: dict[str, Any]
    ) -> None:
        body, hidden = container_body(
            job(hosts=["a.org"]), "r", settings(**changes), expires_at=NOW
        )

        assert body["HostConfig"]["NetworkMode"] == "none"
        assert not [name for name in env_of(body) if "PROXY" in name.upper()]
        assert hidden == ()

    def test_the_proxy_is_told_as_a_program_is_told_of_one(self) -> None:
        assert proxy_url("http://egress:3128", "tok.en") == "http://skill:tok.en@egress:3128"
        assert proxy_url("http://10.0.0.2:3128/", "t") == "http://skill:t@10.0.0.2:3128"


class TestItsEnvironment:
    def test_it_has_its_secrets_and_what_a_program_needs_and_nothing_else(self) -> None:
        env = env_of(body_of(job(env={"HA_TOKEN": "s3cret-value"})))

        assert set(env) == {*FIXED_ENV, "HA_TOKEN"}
        assert env["HA_TOKEN"] == "s3cret-value"

    def test_nothing_of_the_environment_of_the_runner_is_in_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_RUNNER_EGRESS_KEY", "k" * 40)
        monkeypatch.setenv("FACTOTUM_POSTGRES_PASSWORD", "hunter2")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")

        body = body_of(job(env={"HA_TOKEN": "x" * 8}))

        assert "hunter2" not in str(body)
        assert "k" * 40 not in str(body)
        assert "AWS" not in str(body)

    def test_it_does_not_write_bytecode_or_wait_for_a_buffer_to_fill(self) -> None:
        env = env_of(body_of(job()))

        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["HOME"] == env["TMPDIR"] == "/tmp"  # noqa: S108


class TestTheFilesItIsGiven:
    async def test_the_folder_of_the_skill_and_what_starts_it_and_what_it_reads(self) -> None:
        docker = FakeDocker()
        made = job(
            files=[
                JobFile.of("scripts/run.py", b"script"),
                JobFile.of("scripts/helper.py", b"helper"),
                JobFile.of("data/a/b.json", b"{}"),
            ],
            stdin='{"params": 1}',
        )

        await sandbox_of(docker).run(made)

        container = docker.one()
        assert container.files["/skill/scripts/run.py"] == b"script"
        assert container.files["/skill/scripts/helper.py"] == b"helper"
        assert container.files["/skill/data/a/b.json"] == b"{}"
        assert container.files["/opt/factotum/input.json"] == b'{"params": 1}'
        assert b"os.dup2(descriptor, 0)" in container.files["/opt/factotum/shim.py"]
        assert {"/skill", "/skill/scripts", "/skill/data", "/skill/data/a"} <= container.directories

    async def test_all_of_it_is_root_s_and_none_of_it_the_script_s(self) -> None:
        docker = FakeDocker()

        await sandbox_of(docker).run(job())

        assert set(docker.one().owners.values()) == {(0, 0)}

    def test_the_input_is_what_the_script_reads_on_standard_input_and_a_file_holds_it(self) -> None:
        shim = archive_of(job(stdin="hello"))

        assert b"hello" in shim

    def test_a_script_that_reads_its_input_finds_the_shim_there_to_start_it(self) -> None:
        from factotum.runner.sandbox import SHIM

        source = SHIM.decode()
        assert "os.dup2(descriptor, 0)" in source
        assert 'os.execv(sys.executable, [sys.executable, "-E", "-s", sys.argv[1]])' in source
        compile(source, "shim.py", "exec")

    async def test_the_engine_is_never_asked_for_a_root_that_cannot_be_written_to(self) -> None:
        docker = FakeDocker()

        await sandbox_of(docker).run(job())  # the engine refuses files in a read-only root

        assert "ReadonlyRootfs" not in docker.one().body["HostConfig"]


class TestRunning:
    async def test_a_script_that_ends_well_is_what_it_wrote_and_that_it_ended_well(self) -> None:
        docker = FakeDocker(
            Fate(exit_code=0, stdout=b'{"message": "The lamp is on."}', stderr=b"log")
        )

        result = await sandbox_of(docker).run(job())

        assert result.exit_code == 0
        assert result.stdout == '{"message": "The lamp is on."}'
        assert result.stderr == "log"
        assert (result.timed_out, result.truncated, result.oom_killed) == (False, False, False)
        assert result.duration_ms >= 0

    async def test_what_is_asked_of_the_engine_is_asked_in_order_and_ends_with_the_removal(
        self,
    ) -> None:
        docker = FakeDocker()

        await sandbox_of(docker).run(job())

        assert docker.calls == ["create", "put", "start", "wait", "inspect", "logs", "remove"]
        assert docker.one().removed

    async def test_a_script_that_fails_is_a_result_and_not_an_error(self) -> None:
        docker = FakeDocker(Fate(exit_code=1, stdout=b"", stderr=b"Traceback: it broke"))

        result = await sandbox_of(docker).run(job())

        assert (result.exit_code, result.stderr) == (1, "Traceback: it broke")
        assert docker.one().removed

    async def test_a_script_that_asks_for_too_much_memory_is_said_to_have(self) -> None:
        docker = FakeDocker(Fate(exit_code=137, oom=True))

        result = await sandbox_of(docker).run(job())

        assert (result.exit_code, result.oom_killed) == (137, True)

    async def test_a_script_that_takes_too_long_is_killed_and_said_to_have(self) -> None:
        docker = FakeDocker(Fate(hangs=True))

        result = await sandbox_of(docker).run(job(timeout_s=1))

        assert (result.timed_out, result.exit_code) == (True, None)
        container = docker.one()
        assert container.killed
        assert container.removed
        assert docker.calls.index("kill") < docker.calls.index("remove")

    async def test_its_secrets_are_taken_out_of_what_it_wrote(self) -> None:
        docker = FakeDocker(
            Fate(
                stdout=b'{"message": "token is s3cret-value"}',
                stderr=b"HA_TOKEN=s3cret-value failed",
            )
        )

        result = await sandbox_of(docker).run(job(env={"HA_TOKEN": "s3cret-value"}))

        assert "s3cret-value" not in result.stdout + result.stderr
        assert result.stdout == '{"message": "token is ***"}'
        assert result.stderr == "HA_TOKEN=*** failed"

    async def test_the_grant_is_taken_out_of_it_too(self) -> None:
        def says_where_the_proxy_is(body: dict[str, Any]) -> Fate:
            return Fate(stdout=f"proxy is {env_of(body)['HTTP_PROXY']}".encode())

        docker = FakeDocker(says_where_the_proxy_is)

        result = await sandbox_of(docker).run(job(hosts=["a.org"]))

        assert result.stdout == "proxy is http://skill:***@egress:3128"

    async def test_a_secret_too_short_to_be_told_from_other_text_is_left_alone(self) -> None:
        docker = FakeDocker(Fate(stdout=b"the pin is 12 and 123 and 1234"))

        result = await sandbox_of(docker).run(job(env={"PIN": "123"}))

        assert result.stdout == "the pin is 12 and 123 and 1234"

    async def test_more_than_is_kept_of_what_it_wrote_is_cut_and_said(self) -> None:
        docker = FakeDocker(Fate(stdout=b"x" * 500, stderr=b"y" * 10))

        result = await sandbox_of(docker, max_output_chars=100).run(job())

        assert (len(result.stdout), len(result.stderr), result.truncated) == (100, 10, True)

    async def test_what_is_not_text_is_not_an_error(self) -> None:
        docker = FakeDocker(Fate(stdout=b"caf\xe9 \xff\xfe"))

        result = await sandbox_of(docker).run(job())

        assert result.stdout.startswith("caf")
        assert "�" in result.stdout


class TestWhenTheEngineFails:
    @pytest.mark.parametrize("operation", ["create", "put", "start", "wait", "inspect", "logs"])
    async def test_a_job_that_could_not_be_run_is_an_error_and_what_was_made_is_removed(
        self, operation: str
    ) -> None:
        docker = FakeDocker()
        docker.fail[operation] = 500

        with pytest.raises(SandboxUnavailable, match="engine could not run it"):
            await sandbox_of(docker).run(job())

        assert all(container.removed for container in docker.containers.values())
        assert (operation == "create") == (not docker.containers)

    async def test_what_the_engine_said_is_not_what_the_caller_is_told(self) -> None:
        docker = FakeDocker()
        docker.fail["create"] = 500

        with pytest.raises(SandboxUnavailable) as raised:
            await sandbox_of(docker).run(job())

        assert "create failed" not in str(raised.value)
        assert "500" not in str(raised.value)

    async def test_an_engine_that_is_not_there_is_the_same(self) -> None:
        docker = FakeDocker()
        docker.down = True

        with pytest.raises(SandboxUnavailable):
            await sandbox_of(docker).run(job())

    async def test_a_runner_that_had_a_failure_goes_on_with_the_next_job(self) -> None:
        docker = FakeDocker()
        runner = sandbox_of(docker, max_concurrency=1)
        docker.fail["start"] = 500
        with pytest.raises(SandboxUnavailable):
            await runner.run(job())
        del docker.fail["start"]

        result = await runner.run(job())

        assert result.exit_code == 0


class TestWhenThereAreMoreJobsThanItRuns:
    async def test_the_one_that_cannot_wait_is_told_that_it_is_busy(self) -> None:
        docker = FakeDocker(Fate(hangs=True))
        runner = sandbox_of(docker, max_concurrency=1, queue_wait_s=0.1)
        first = asyncio.create_task(runner.run(job(timeout_s=30)))
        await asyncio.wait_for(docker.wait_started.wait(), 5)

        with pytest.raises(SandboxUnavailable, match="busy"):
            await runner.run(job())

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    async def test_one_that_can_wait_runs_when_the_other_is_done(self) -> None:
        docker = FakeDocker()
        runner = sandbox_of(docker, max_concurrency=1, queue_wait_s=5)

        results = await asyncio.gather(runner.run(job()), runner.run(job()), runner.run(job()))

        assert [result.exit_code for result in results] == [0, 0, 0]
        assert len(docker.containers) == 3

    async def test_no_more_than_it_may_run_at_once_are_running(self) -> None:
        docker = FakeDocker()
        docker.wait_delay_s = 0.05
        runner = sandbox_of(docker, max_concurrency=2, queue_wait_s=5)

        await asyncio.gather(*(runner.run(job()) for _ in range(6)))

        assert docker.peak == 2


class TestWhenTheCallerGoesAway:
    async def test_the_container_is_removed_and_the_place_it_had_is_free_again(self) -> None:
        docker = FakeDocker(Fate(hangs=True))
        runner = sandbox_of(docker, max_concurrency=1, queue_wait_s=0.1)
        task = asyncio.create_task(runner.run(job(timeout_s=30)))
        await asyncio.wait_for(docker.wait_started.wait(), 5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert docker.one().removed
        docker.fate = Fate()
        assert (await runner.run(job())).exit_code == 0


class TestWhetherItCanRunJobs:
    async def test_it_can_when_the_engine_answers_and_has_the_image(self) -> None:
        assert await sandbox_of(FakeDocker()).ready()

    async def test_it_cannot_when_the_image_is_not_there(self) -> None:
        docker = FakeDocker()
        docker.image_present = False

        assert not await sandbox_of(docker).ready()

    async def test_it_cannot_when_the_engine_does_not_answer(self) -> None:
        docker = FakeDocker()
        docker.down = True

        assert not await sandbox_of(docker).ready()

    async def test_it_cannot_when_the_engine_is_in_a_bad_way(self) -> None:
        docker = FakeDocker()
        docker.fail["ping"] = 500

        assert not await sandbox_of(docker).ready()


class TestCleaningUp:
    async def test_what_a_runner_that_died_left_is_removed_and_what_may_be_running_is_not(
        self,
    ) -> None:
        clock = [float(NOW)]
        docker = FakeDocker()
        docker.now = lambda: clock[0]
        engine = docker.engine()
        labels = {"factotum.sandbox": "1"}
        old = await engine.create("old", {"Labels": labels})
        clock[0] += 250
        young = await engine.create("young", {"Labels": labels})
        other = await engine.create("other", {"Labels": {"something": "else"}})
        clock[0] += 100
        runner = Sandbox(engine, settings(), now=lambda: clock[0])

        removed = await runner.clean_up()

        assert removed == 1
        assert docker.containers[old].removed
        assert not docker.containers[young].removed
        assert not docker.containers[other].removed

    async def test_an_engine_that_cannot_say_leaves_things_as_they_are(self) -> None:
        docker = FakeDocker()
        docker.fail["list"] = 500

        assert await sandbox_of(docker).clean_up() == 0
