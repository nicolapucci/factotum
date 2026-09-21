"""A container for each run of a script, and what it is like.

The script of a skill is somebody's code and it runs in a container that is made for it and thrown
away after it. What the container is like is decided here, in one place, and a job cannot change it:
it names its files, what it reads, its secrets, the hosts it may reach and how long it has, and
nothing else.

* **It runs as nobody in particular**, with no capability and no way to get one, and a limit on the
  memory, the processes and the CPU it may use. Docker refuses to put files into a container whose
  root is marked read-only, so the flag is not used; what it is for is done otherwise: the files are
  owned by root and the script is not, and the only place it may write is a small ``/tmp`` in memory
  that cannot run programs.
* **It has no network unless it is granted hosts.** With none, it is on no network at all. With
  some, it is on a network that has nothing on it but the egress proxy, and it is given the grant
  that says which hosts the proxy lets it reach.
* **Its secrets are its environment**, only the ones it was granted, and nothing of the runner's.
* **It is always removed**, whatever became of it: it ended, it was too slow and was killed, the
  engine failed half way, the caller went away.

What it wrote is not trusted and not shown as it is: the values of its secrets are taken out of it.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from factotum.common import egress
from factotum.contracts.runner import MAX_OUTPUT_CHARS, Job, JobResult
from factotum.runner.docker import DockerEngine, DockerError, tar_of

logger = logging.getLogger("factotum.runner")

SHIM_PATH = "/opt/factotum/shim.py"
INPUT_PATH = "/opt/factotum/input.json"
SKILL_DIR = "/skill"

SHIM = b'''"""Gives the script its input on standard input and starts it."""
import os
import sys

descriptor = os.open("/opt/factotum/input.json", os.O_RDONLY)
os.dup2(descriptor, 0)
os.close(descriptor)
os.execv(sys.executable, [sys.executable, "-E", "-s", sys.argv[1]])
'''

FIXED_ENV = {
    "HOME": "/tmp",  # noqa: S108 - the memory of the container, not a place on a machine
    "TMPDIR": "/tmp",  # noqa: S108
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}

MAX_LIFETIME_S = 300
"""A container of the runner that is older than this is not one that is running a job."""


class SandboxUnavailable(Exception):  # noqa: N818 - it says that nothing can run a job now
    """A job could not be run: the engine could not be reached or refused, or the runner is busy."""


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    image: str = "python:3.13-slim"
    network: str = "factotum-sandbox"
    """The network that has nothing on it but the egress proxy."""

    egress_url: str | None = "http://egress:3128"
    """Where the proxy is, for a container on that network. Without it a job has no network."""

    egress_key: bytes | None = None
    memory_mb: int = 128
    cpus: float = 0.5
    pids: int = 64
    tmp_mb: int = 16
    user: str = "10002:10002"
    log_kb: int = 256
    """How much of what a container wrote the engine keeps: a script that writes without end
    cannot fill a disk."""

    max_output_chars: int = MAX_OUTPUT_CHARS
    grace_s: float = 10.0
    """What is allowed for a container to be made and started, and for what is left of it to be
    cleaned away, beyond the time the script has."""

    max_concurrency: int = 4
    queue_wait_s: float = 5.0
    """How long a job waits for its turn before it is told that the runner is busy."""

    label: str = "factotum.sandbox"


def proxy_url(egress_url: str, grant: str) -> str:
    """The address of the proxy with the grant as its password, as a program is told of it."""
    parts = urlsplit(egress_url)
    return f"{parts.scheme}://{egress.USER}:{grant}@{parts.netloc}"


def archive_of(job: Job) -> bytes:
    """The files that go into the container: the folder of the skill, and what starts it."""
    entries: dict[str, bytes | None] = {"skill": None, "opt": None, "opt/factotum": None}
    for file in job.files:
        parent = ""
        for part in file.path.split("/")[:-1]:
            parent = f"{parent}/{part}" if parent else part
            entries.setdefault(f"skill/{parent}", None)
        entries[f"skill/{file.path}"] = file.data
    entries["opt/factotum/shim.py"] = SHIM
    entries["opt/factotum/input.json"] = job.stdin.encode("utf-8")
    return tar_of(entries)


def container_body(
    job: Job, run: str, settings: SandboxSettings, *, expires_at: int
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """What is asked of the engine for a job, and the values that are not to be shown of what it
    writes (its secrets, and the grant)."""
    env = {**FIXED_ENV, **job.env}
    secret = [value for value in job.env.values() if len(value) >= 4]
    url, key = settings.egress_url, settings.egress_key
    reaches = bool(job.hosts) and url is not None and key is not None
    if reaches and url is not None and key is not None:
        grant = egress.issue(key, run=run, hosts=job.hosts, expires_at=expires_at)
        address = proxy_url(url, grant)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[name] = address
        secret.append(grant)
    body: dict[str, Any] = {
        "Image": settings.image,
        "Cmd": ["python", "-E", "-s", SHIM_PATH, f"{SKILL_DIR}/{job.entrypoint}"],
        "Env": [f"{name}={value}" for name, value in env.items()],
        "User": settings.user,
        "WorkingDir": SKILL_DIR,
        "Labels": {settings.label: "1", "factotum.run": run, "factotum.job": job.label},
        "AttachStdin": False,
        "AttachStdout": False,
        "AttachStderr": False,
        "OpenStdin": False,
        "Tty": False,
        "StopTimeout": 1,
        "HostConfig": {
            "NetworkMode": settings.network if reaches else "none",
            "Privileged": False,
            "PublishAllPorts": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": settings.memory_mb * 1024 * 1024,
            "MemorySwap": settings.memory_mb * 1024 * 1024,
            "NanoCpus": int(settings.cpus * 1_000_000_000),
            "PidsLimit": settings.pids,
            "Tmpfs": {"/tmp": f"rw,noexec,nosuid,nodev,size={settings.tmp_mb}m"},  # noqa: S108
            "IpcMode": "none",
            "AutoRemove": False,
            "Ulimits": [
                {"Name": "nofile", "Soft": 256, "Hard": 256},
                {"Name": "core", "Soft": 0, "Hard": 0},
            ],
            "LogConfig": {
                "Type": "json-file",
                "Config": {"max-size": f"{settings.log_kb}k", "max-file": "1"},
            },
        },
    }
    return body, tuple(secret)


def _shown(raw: bytes, hidden: tuple[str, ...], limit: int) -> str:
    text = raw.decode("utf-8", errors="replace")
    for value in hidden:
        text = text.replace(value, "***")
    return text[:limit]


class Sandbox:
    def __init__(
        self,
        engine: DockerEngine,
        settings: SandboxSettings,
        *,
        now: Callable[[], float] = time.time,
        new_run: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._now = now
        self._new_run = new_run
        self._slots = asyncio.Semaphore(settings.max_concurrency)

    async def ready(self) -> bool:
        """Whether jobs can be run: the engine answers and has the image."""
        try:
            await self._engine.ping()
            return await self._engine.has_image(self._settings.image)
        except DockerError:
            return False

    async def run(self, job: Job) -> JobResult:
        """What a job did. Raises ``SandboxUnavailable`` when it could not be run at all, which is
        a different thing from a script that failed: that is a result."""
        run = self._new_run()
        expires_at = int(self._now()) + job.timeout_s + int(self._settings.grace_s) + 5
        body, hidden = container_body(job, run, self._settings, expires_at=expires_at)
        try:
            async with asyncio.timeout(self._settings.queue_wait_s):
                await self._slots.acquire()
        except TimeoutError:
            raise SandboxUnavailable("the runner is busy") from None
        container: str | None = None
        began = time.monotonic()
        try:
            container = await self._engine.create(f"factotum-sandbox-{run}", body)
            await self._engine.put(container, archive_of(job))
            await self._engine.start(container)
            timed_out = await self._wait(container, job.timeout_s)
            state = await self._engine.inspect(container)
            outputs, cut = await self._engine.logs(
                container, limit=self._settings.max_output_chars * 4
            )
        except DockerError as error:
            logger.warning(
                "a job could not be run", extra={"job": job.label, "reason": str(error)[:200]}
            )
            raise SandboxUnavailable("the engine could not run it") from None
        finally:
            self._slots.release()
            if container is not None:
                await asyncio.shield(self._engine.remove(container))
        limit = self._settings.max_output_chars
        stdout, stderr = (_shown(outputs[name], hidden, limit) for name in ("stdout", "stderr"))
        return JobResult(
            exit_code=None if timed_out else state.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=cut or len(outputs["stdout"]) > limit or len(outputs["stderr"]) > limit,
            oom_killed=state.oom_killed,
            duration_ms=int((time.monotonic() - began) * 1000),
        )

    async def _wait(self, container: str, timeout_s: int) -> bool:
        """Until the container has ended, or until it has had all the time it has and is killed.
        True when it was too slow."""
        try:
            async with asyncio.timeout(timeout_s):
                await self._engine.wait(container)
        except TimeoutError:
            await self._engine.kill(container)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._settings.grace_s):
                    await self._engine.wait(container)
            return True
        return False

    async def clean_up(self, *, older_than_s: float = MAX_LIFETIME_S) -> int:
        """Remove the containers of the runner that are older than any job could be: what a runner
        that died left behind. Returns how many were removed."""
        removed = 0
        try:
            found = await self._engine.leftovers(self._settings.label)
        except DockerError:
            return 0
        for item in found:
            created = item.get("Created")
            identifier = item.get("Id")
            if not isinstance(identifier, str) or not isinstance(created, int | float):
                continue
            if self._now() - created > older_than_s:
                await self._engine.remove(identifier)
                removed += 1
        return removed
