"""The sandbox on a real engine: that a container is what the runner says it is, from the inside.

What the tests of the sandbox on an engine of Python show is what is asked for. These show what is
got: a script that looks at itself (who it is, what it may do, what it can reach), and scripts that
misbehave (they use all the memory, all the processes, all the time, write without end), and that
nothing is left behind. They need the socket of an engine and the image the scripts run in (which
is never pulled here), and they skip without either: on Windows the engine is not on a socket, so
they run in a Linux container that is given one (see docs/runner.md).
"""

import json
import os
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

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


def sandbox_of(engine: DockerEngine, **changes: object) -> Sandbox:
    options: dict[str, object] = {"image": IMAGE, "label": "factotum.sandbox.test", "grace_s": 15}
    return Sandbox(engine, SandboxSettings(**{**options, **changes}))  # type: ignore[arg-type]


async def run(
    engine: DockerEngine,
    source: str,
    *,
    stdin: str = "",
    env: dict[str, str] | None = None,
    timeout_s: int = 20,
    **changes: object,
) -> JobResult:
    job = Job(
        label="test",
        files=(JobFile.of("scripts/run.py", textwrap.dedent(source).encode()),),
        entrypoint="scripts/run.py",
        stdin=stdin,
        env=env or {},
        timeout_s=timeout_s,
    )
    return await sandbox_of(engine, **changes).run(job)


def said(result: JobResult) -> dict[str, object]:
    assert result.exit_code == 0, result.stderr
    loaded: dict[str, object] = json.loads(result.stdout)
    return loaded


async def left(engine: DockerEngine) -> list[dict[str, object]]:
    return await engine.leftovers("factotum.sandbox.test")


class TestARunThatGoesWell:
    async def test_the_script_reads_what_it_is_given_and_says_what_it_did(
        self, engine: DockerEngine
    ) -> None:
        result = await run(
            engine,
            """
            import json, sys
            request = json.load(sys.stdin)
            print(json.dumps({"message": "Hello " + request["params"]["name"]}))
            """,
            stdin='{"params": {"name": "Ada"}}',
        )

        assert said(result) == {"message": "Hello Ada"}
        assert result.stderr == ""
        assert 0 < result.duration_ms < 20_000

    async def test_what_the_script_writes_to_the_other_output_is_kept_apart(
        self, engine: DockerEngine
    ) -> None:
        result = await run(engine, 'import sys\nprint("out")\nprint("err", file=sys.stderr)\n')

        assert (result.stdout.strip(), result.stderr.strip()) == ("out", "err")

    async def test_a_script_that_fails_says_how(self, engine: DockerEngine) -> None:
        result = await run(engine, "raise SystemExit(3)")

        assert (result.exit_code, result.timed_out, result.oom_killed) == (3, False, False)

    async def test_a_script_can_use_the_files_beside_it(self, engine: DockerEngine) -> None:
        job = Job(
            label="test",
            files=(
                JobFile.of(
                    "scripts/run.py", b"import helper, json\nprint(json.dumps(helper.VALUE))\n"
                ),
                JobFile.of("scripts/helper.py", b"VALUE = {'from': 'a helper'}\n"),
            ),
            entrypoint="scripts/run.py",
            timeout_s=20,
        )

        result = await sandbox_of(engine).run(job)

        assert said(result) == {"from": "a helper"}


class TestWhoTheScriptIs:
    async def test_nobody_in_particular_with_no_power_and_no_way_to_get_any(
        self, engine: DockerEngine
    ) -> None:
        result = await run(
            engine,
            """
            import json, os
            lines = open("/proc/self/status").read().splitlines()
            status = dict(line.split(":\\t", 1) for line in lines if ":\\t" in line)
            print(json.dumps({
                "uid": os.getuid(), "gid": os.getgid(), "euid": os.geteuid(),
                "capabilities": status["CapEff"].strip(),
                "no_new_privs": status["NoNewPrivs"].strip(),
            }))
            """,
        )

        assert said(result) == {
            "uid": 10002,
            "gid": 10002,
            "euid": 10002,
            "capabilities": "0000000000000000",
            "no_new_privs": "1",
        }

    async def test_it_cannot_change_what_it_is_given_or_the_system_and_can_use_a_small_scratch(
        self, engine: DockerEngine
    ) -> None:
        result = await run(
            engine,
            """
            import json
            outcomes = {}
            paths = ["/skill/scripts/run.py", "/skill/new.txt", "/opt/factotum/shim.py"]
            for path in paths + ["/etc/passwd", "/usr/local/bin/x"]:
                try:
                    open(path, "a").close()
                    outcomes[path] = "written"
                except OSError as error:
                    outcomes[path] = type(error).__name__
            with open("/tmp/scratch.txt", "w") as scratch:
                scratch.write("ok")
            outcomes["/tmp"] = open("/tmp/scratch.txt").read()
            print(json.dumps(outcomes))
            """,
        )

        outcomes = said(result)
        scratch = outcomes.pop("/tmp")  # noqa: S108 - the scratch of the container
        assert scratch == "ok"
        assert outcomes == {
            "/skill/scripts/run.py": "PermissionError",
            "/skill/new.txt": "PermissionError",
            "/opt/factotum/shim.py": "PermissionError",
            "/etc/passwd": "PermissionError",
            "/usr/local/bin/x": "PermissionError",
        }

    async def test_nothing_in_the_scratch_can_be_run(self, engine: DockerEngine) -> None:
        result = await run(
            engine,
            """
            import json, os, stat
            path = "/tmp/program"
            open(path, "w").write("#!/bin/sh\\necho hi\\n")
            os.chmod(path, 0o755)
            try:
                os.execv(path, [path])
            except OSError as error:
                print(json.dumps({"error": type(error).__name__}))
            """,
        )

        assert said(result) == {"error": "PermissionError"}


class TestWhatTheScriptIsGiven:
    async def test_its_secrets_and_what_a_program_needs_and_nothing_of_the_runner(
        self, engine: DockerEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTOTUM_RUNNER_EGRESS_KEY", "must-never-reach-a-script-0123456789")

        result = await run(
            engine,
            "import json, os\nprint(json.dumps(sorted(os.environ)))\n",
            env={"HA_TOKEN": "s3cret-value"},
        )

        names = said(result)
        assert isinstance(names, list)
        allowed = {"HA_TOKEN", "HOME", "TMPDIR", "LANG", "PATH", "HOSTNAME", "GPG_KEY"}
        allowed |= {"PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}
        assert "HA_TOKEN" in names
        assert not [n for n in names if n not in allowed and not n.startswith("PYTHON")]
        assert not [n for n in names if n.startswith("FACTOTUM") or "PROXY" in n]
        assert "must-never-reach" not in result.stdout

    async def test_the_secret_is_there_for_the_script_and_gone_from_what_it_says(
        self, engine: DockerEngine
    ) -> None:
        result = await run(
            engine,
            'import os\nprint("the token is", os.environ["HA_TOKEN"])\n',
            env={"HA_TOKEN": "s3cret-value"},
        )

        assert result.stdout.strip() == "the token is ***"


class TestWhatTheScriptCanReach:
    async def test_with_no_hosts_nothing_at_all(self, engine: DockerEngine) -> None:
        result = await run(
            engine,
            """
            import json, os, socket
            outcomes = {"interfaces": sorted(os.listdir("/sys/class/net"))}
            targets = (("a public address", ("1.1.1.1", 53)), ("a name", ("example.org", 80)))
            for name, address in targets:
                try:
                    socket.create_connection(address, timeout=3).close()
                    outcomes[name] = "connected"
                except OSError as error:
                    outcomes[name] = type(error).__name__
            print(json.dumps(outcomes))
            """,
        )

        outcomes = said(result)
        assert outcomes["interfaces"] == ["lo"]
        assert outcomes["a public address"] != "connected"
        assert outcomes["a name"] != "connected"


class TestAScriptThatMisbehaves:
    async def test_one_that_uses_all_the_memory_is_stopped_and_said_to_have(
        self, engine: DockerEngine
    ) -> None:
        result = await run(
            engine,
            "hog = []\nwhile True:\n    hog.append(bytearray(10_000_000))\n",
            memory_mb=64,
        )

        assert result.oom_killed
        assert result.exit_code == 137

    async def test_one_that_starts_more_processes_than_it_may_is_refused_them(
        self, engine: DockerEngine
    ) -> None:
        result = await run(
            engine,
            """
            import json, os, time
            started = 0
            children = []
            try:
                for _ in range(500):
                    pid = os.fork()
                    if pid == 0:
                        time.sleep(30)
                        os._exit(0)
                    children.append(pid)
                    started += 1
            except OSError as error:
                print(json.dumps({"started": started, "error": type(error).__name__}))
            finally:
                for pid in children:
                    os.kill(pid, 9)
            """,
            pids=32,
        )

        outcome = said(result)
        assert 0 < int(outcome["started"]) < 32  # type: ignore[call-overload]
        assert outcome["error"] == "BlockingIOError"

    async def test_one_that_takes_too_long_is_stopped_and_said_to_have(
        self, engine: DockerEngine
    ) -> None:
        result = await run(engine, "while True:\n    pass\n", timeout_s=2)

        assert (result.timed_out, result.exit_code) == (True, None)
        assert 1_500 < result.duration_ms < 15_000

    async def test_one_that_writes_without_end_gets_a_result_that_says_it_did(
        self, engine: DockerEngine
    ) -> None:
        result = await run(
            engine,
            'import sys\nfor _ in range(200_000):\n    sys.stdout.write("x" * 100 + "\\n")\n',
            max_output_chars=2000,
            log_kb=64,
        )

        assert result.truncated
        assert len(result.stdout) <= 2000

    async def test_one_that_fills_the_scratch_is_refused_more_and_not_the_disk(
        self, engine: DockerEngine
    ) -> None:
        result = await run(
            engine,
            """
            import json
            written = 0
            try:
                with open("/tmp/fill", "wb") as target:
                    while True:
                        target.write(b"x" * 1_000_000)
                        target.flush()
                        written += 1_000_000
            except OSError as error:
                print(json.dumps({"error": type(error).__name__, "mb": written // 1_000_000}))
            """,
            tmp_mb=4,
        )

        outcome = said(result)
        assert outcome["error"] == "OSError"
        assert int(outcome["mb"]) <= 4  # type: ignore[call-overload]


class TestWhatIsLeftBehind:
    async def test_nothing_whatever_became_of_the_script(self, engine: DockerEngine) -> None:
        await run(engine, "print('{}')")
        await run(engine, "raise SystemExit(1)")
        await run(engine, "while True:\n    pass\n", timeout_s=1)
        await run(engine, "bytearray(2_000_000_000)", memory_mb=32)

        assert await left(engine) == []

    async def test_what_a_runner_that_died_left_is_found_and_removed(
        self, engine: DockerEngine
    ) -> None:
        labels = {"factotum.sandbox.test": "1"}
        leftover = await engine.create(
            "factotum-sandbox-test-leftover",
            {"Image": IMAGE, "Cmd": ["true"], "Labels": labels},
        )

        removed = await sandbox_of(engine).clean_up(older_than_s=-1)

        assert removed == 1
        assert await left(engine) == []
        assert leftover
