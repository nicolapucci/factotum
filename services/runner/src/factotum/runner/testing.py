"""A Docker engine that is made of Python, so that what the runner asks of one can be tested without
one: it keeps the containers it is asked to make, the files that are put in them and the order of
what it is asked, answers what a test says a container did, and refuses what a real engine refuses
(files in a container whose root is read-only, a name that is taken)."""

import asyncio
import io
import json
import re
import struct
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from factotum.runner.docker import API, DockerEngine


@dataclass
class Fate:
    """What a container does."""

    exit_code: int = 0
    stdout: bytes = b"{}"
    stderr: bytes = b""
    oom: bool = False
    hangs: bool = False
    """It never ends by itself: it ends when it is killed."""


@dataclass
class Made:
    """A container the engine was asked to make."""

    id: str
    name: str
    body: dict[str, Any]
    fate: Fate
    files: dict[str, bytes] = field(default_factory=dict)
    directories: set[str] = field(default_factory=set)
    owners: dict[str, tuple[int, int]] = field(default_factory=dict)
    started: bool = False
    killed: bool = False
    removed: bool = False
    created: float = 0.0
    ended: asyncio.Event = field(default_factory=asyncio.Event)


def frame(stream: int, data: bytes) -> bytes:
    """A piece of what a container wrote, as the engine says it."""
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(data)) + data


class FakeDocker:
    def __init__(self, fate: Fate | Callable[[dict[str, Any]], Fate] | None = None) -> None:
        self.fate = fate or Fate()
        self.containers: dict[str, Made] = {}
        self.calls: list[str] = []
        """What was asked, in order: ``create``, ``put``, ``start``, ``wait``, ``inspect``,
        ``logs``, ``kill``, ``remove``..."""

        self.fail: dict[str, int] = {}
        """An operation that the engine answers with this status."""

        self.image_present = True
        self.down = False
        self.now = time.time
        self.wait_started = asyncio.Event()
        self.wait_delay_s = 0.0
        """How long a container that is waited for takes to end, for a test of how many run."""

        self.waiting = 0
        self.peak = 0
        """How many are being waited for at once, and the most that ever were."""

    # ---- the client ---------------------------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), base_url="http://docker"
        )

    def engine(self) -> DockerEngine:
        return DockerEngine(self.client())

    # ---- the engine ---------------------------------------------------------------------------

    def _fate_for(self, body: dict[str, Any]) -> Fate:
        return self.fate(body) if callable(self.fate) else self.fate

    def one(self, name: str = "") -> Made:
        """The container that was made (the only one, or the one with that in its name)."""
        found = [c for c in self.containers.values() if name in c.name]
        assert len(found) == 1, f"{len(found)} containers"  # noqa: S101
        return found[0]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("the engine is not there")
        path = request.url.path.removeprefix(API)
        method = request.method
        for pattern, handler in self._routes:
            match = re.fullmatch(pattern, path)
            if match and method == handler[0]:
                operation = handler[1]
                self.calls.append(operation)
                if operation in self.fail:
                    return httpx.Response(
                        self.fail[operation], json={"message": f"{operation} failed"}
                    )
                return await handler[2](self, request, *match.groups())
        return httpx.Response(404, json={"message": f"no such route: {method} {path}"})

    async def _ping(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="OK")

    async def _image(self, request: httpx.Request, name: str) -> httpx.Response:
        if self.image_present:
            return httpx.Response(200, json={"Id": "sha256:image"})
        return httpx.Response(404, json={"message": "No such image"})

    async def _create(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("name", "")
        if any(c.name == name and not c.removed for c in self.containers.values()):
            return httpx.Response(409, json={"message": "the name is taken"})
        body = json.loads(request.content)
        identifier = f"{len(self.containers) + 1:04d}" + "a" * 60
        self.containers[identifier] = Made(
            identifier, name, body, self._fate_for(body), created=self.now()
        )
        return httpx.Response(201, json={"Id": identifier, "Warnings": []})

    def _made(self, identifier: str) -> Made | None:
        made = self.containers.get(identifier)
        return None if made is None or made.removed else made

    async def _put(self, request: httpx.Request, identifier: str) -> httpx.Response:
        made = self._made(identifier)
        if made is None:
            return httpx.Response(404, json={"message": "No such container"})
        if made.body.get("HostConfig", {}).get("ReadonlyRootfs"):
            return httpx.Response(403, json={"message": "container rootfs is marked read-only"})
        with tarfile.open(fileobj=io.BytesIO(request.content)) as archive:
            for member in archive:
                path = "/" + member.name.rstrip("/")
                made.owners[path] = (member.uid, member.gid)
                if member.isdir():
                    made.directories.add(path)
                else:
                    extracted = archive.extractfile(member)
                    made.files[path] = b"" if extracted is None else extracted.read()
        return httpx.Response(200)

    async def _start(self, request: httpx.Request, identifier: str) -> httpx.Response:
        made = self._made(identifier)
        if made is None:
            return httpx.Response(404, json={"message": "No such container"})
        made.started = True
        if not made.fate.hangs:
            made.ended.set()
        return httpx.Response(204)

    async def _wait(self, request: httpx.Request, identifier: str) -> httpx.Response:
        made = self._made(identifier)
        if made is None:
            return httpx.Response(404, json={"message": "No such container"})
        self.wait_started.set()
        self.waiting += 1
        self.peak = max(self.peak, self.waiting)
        try:
            if self.wait_delay_s:
                await asyncio.sleep(self.wait_delay_s)
            await made.ended.wait()
        finally:
            self.waiting -= 1
        return httpx.Response(200, json={"StatusCode": 137 if made.killed else made.fate.exit_code})

    async def _kill(self, request: httpx.Request, identifier: str) -> httpx.Response:
        made = self._made(identifier)
        if made is None:
            return httpx.Response(404, json={"message": "No such container"})
        made.killed = True
        made.ended.set()
        return httpx.Response(204)

    async def _inspect(self, request: httpx.Request, identifier: str) -> httpx.Response:
        made = self._made(identifier)
        if made is None:
            return httpx.Response(404, json={"message": "No such container"})
        code = 137 if made.killed else made.fate.exit_code
        state = {"ExitCode": code, "OOMKilled": made.fate.oom, "Error": ""}
        return httpx.Response(200, json={"Id": identifier, "State": state})

    async def _logs(self, request: httpx.Request, identifier: str) -> httpx.Response:
        made = self._made(identifier)
        if made is None:
            return httpx.Response(404, json={"message": "No such container"})
        content = b""
        if made.fate.stdout:
            content += frame(1, made.fate.stdout)
        if made.fate.stderr:
            content += frame(2, made.fate.stderr)
        return httpx.Response(200, content=content)

    async def _remove(self, request: httpx.Request, identifier: str) -> httpx.Response:
        made = self.containers.get(identifier)
        if made is None or made.removed:
            return httpx.Response(404, json={"message": "No such container"})
        made.removed = True
        made.ended.set()
        return httpx.Response(204)

    async def _list(self, request: httpx.Request) -> httpx.Response:
        wanted = json.loads(request.url.params.get("filters", "{}")).get("label", [])
        found = [
            {"Id": made.id, "Created": int(made.created), "Labels": made.body.get("Labels", {})}
            for made in self.containers.values()
            if not made.removed and all(label in made.body.get("Labels", {}) for label in wanted)
        ]
        return httpx.Response(200, json=found)

    _routes = (
        (r"/_ping", ("GET", "ping", _ping)),
        (r"/images/(.+)/json", ("GET", "image", _image)),
        (r"/containers/create", ("POST", "create", _create)),
        (r"/containers/json", ("GET", "list", _list)),
        (r"/containers/([0-9a-f]+)/archive", ("PUT", "put", _put)),
        (r"/containers/([0-9a-f]+)/start", ("POST", "start", _start)),
        (r"/containers/([0-9a-f]+)/wait", ("POST", "wait", _wait)),
        (r"/containers/([0-9a-f]+)/kill", ("POST", "kill", _kill)),
        (r"/containers/([0-9a-f]+)/json", ("GET", "inspect", _inspect)),
        (r"/containers/([0-9a-f]+)/logs", ("GET", "logs", _logs)),
        (r"/containers/([0-9a-f]+)", ("DELETE", "remove", _remove)),
    )
