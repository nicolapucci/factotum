"""The little of the Docker Engine API that the runner needs.

A container for every run is made through the API of the engine that the runner is given, and it is
the only thing the runner asks of it: to make a container from an image it names, to put files in
it, to start it, to wait for it, to read what it wrote, and to remove it. The API is spoken over
HTTP on the socket of the engine (or a TCP port, for an engine that is elsewhere), which is why the
runner needs no SDK: ``httpx`` is already how everything else here speaks HTTP.

Nothing here decides what a container is like: ``sandbox`` makes the body of the request and this
sends it.
"""

import contextlib
import io
import json
import struct
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

API = "/v1.41"
"""The version of the API that is asked for: the oldest that has all of this, so that an engine that
is a few years old (or a compatible one, such as Podman's) is enough."""

_STREAMS = {1: "stdout", 2: "stderr"}


class DockerError(Exception):
    """The engine did not do what was asked, or could not be reached."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Exited:
    """How a container ended."""

    exit_code: int
    oom_killed: bool
    error: str


def parse_host(host: str) -> tuple[str | None, str]:
    """Where the engine is: the path of a socket, and the address to give the client, from
    ``unix:///var/run/docker.sock`` or ``tcp://host:2375`` (the two forms that ``DOCKER_HOST``
    has)."""
    if host.startswith("unix://"):
        return host.removeprefix("unix://"), "http://docker"
    if host.startswith("tcp://"):
        return None, "http://" + host.removeprefix("tcp://")
    if host.startswith(("http://", "https://")):
        return None, host
    raise ValueError("the engine is at a unix:// socket or a tcp:// address")


def tar_of(entries: Mapping[str, bytes | None]) -> bytes:
    """A tar of ``entries`` (a path, and its content, or nothing for a directory), owned by root and
    readable by everybody, that the engine unpacks in a container: what a script is given is not
    something that it can change."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, content in entries.items():
            info = tarfile.TarInfo(path.rstrip("/") + ("/" if content is None else ""))
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(content))
    return out.getvalue()


def demultiplex(raw: bytes, *, limit: int) -> tuple[dict[str, bytes], bool]:
    """What a container wrote to each of its two outputs, from the stream that the engine gives
    them in (a header of eight bytes for each piece: which output, and how long it is), and whether
    more was written than the ``limit`` that is kept of each."""
    kept = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = False
    position = 0
    while position + 8 <= len(raw):
        kind = raw[position]
        (size,) = struct.unpack(">I", raw[position + 4 : position + 8])
        piece = raw[position + 8 : position + 8 + size]
        position += 8 + size
        name = _STREAMS.get(kind)
        if name is None:
            continue
        room = limit - len(kept[name])
        if len(piece) > room:
            truncated = True
            piece = piece[: max(room, 0)]
        kept[name] += piece
    return {name: bytes(data) for name, data in kept.items()}, truncated


class DockerEngine:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
        patience: httpx.Timeout | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                API + path,
                params=params,
                json=json_body,
                content=content,
                timeout=patience if patience is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as error:
            raise DockerError(f"the engine could not be reached ({type(error).__name__})") from None
        if response.status_code not in ok:
            raise DockerError(
                f"the engine said {response.status_code}: {_message_of(response)}",
                status=response.status_code,
            )
        return response

    async def ping(self) -> None:
        await self._call("GET", "/_ping", ok=(200,))

    async def has_image(self, image: str) -> bool:
        try:
            await self._call("GET", f"/images/{quote(image, safe='')}/json", ok=(200,))
        except DockerError as error:
            if error.status == 404:
                return False
            raise
        return True

    async def create(self, name: str, body: Mapping[str, Any]) -> str:
        response = await self._call(
            "POST", "/containers/create", params={"name": name}, json_body=dict(body), ok=(201,)
        )
        identifier = response.json().get("Id")
        if not isinstance(identifier, str) or not identifier:
            raise DockerError("the engine made a container and did not say which")
        return identifier

    async def put(self, container: str, archive: bytes) -> None:
        await self._call(
            "PUT", f"/containers/{container}/archive", params={"path": "/"}, content=archive
        )

    async def start(self, container: str) -> None:
        await self._call("POST", f"/containers/{container}/start", ok=(204, 304))

    async def wait(self, container: str) -> int:
        """The exit code of a container, once it has ended (at once, if it has)."""
        response = await self._call(
            "POST",
            f"/containers/{container}/wait",
            params={"condition": "not-running"},
            patience=httpx.Timeout(None),
        )
        code = response.json().get("StatusCode")
        return int(code) if isinstance(code, int) else -1

    async def kill(self, container: str) -> None:
        with contextlib.suppress(DockerError):  # it may have ended by itself in the meantime
            await self._call("POST", f"/containers/{container}/kill", ok=(204, 404, 409))

    async def inspect(self, container: str) -> Exited:
        state = (
            (await self._call("GET", f"/containers/{container}/json", ok=(200,)))
            .json()
            .get("State", {})
        )
        return Exited(
            exit_code=int(state.get("ExitCode", -1)),
            oom_killed=bool(state.get("OOMKilled", False)),
            error=str(state.get("Error", "")),
        )

    async def logs(self, container: str, *, limit: int) -> tuple[dict[str, bytes], bool]:
        """What the container wrote, as far as ``limit`` for each output, and whether there was
        more. What is read is never more than the engine kept (its log is limited when the
        container is made)."""
        response = await self._call(
            "GET",
            f"/containers/{container}/logs",
            params={"stdout": "1", "stderr": "1", "follow": "0"},
            ok=(200,),
        )
        return demultiplex(response.content, limit=limit)

    async def remove(self, container: str) -> None:
        with contextlib.suppress(DockerError):
            await self._call(
                "DELETE",
                f"/containers/{container}",
                params={"force": "1", "v": "1"},
                ok=(200, 204, 404),
            )

    async def leftovers(self, label: str) -> list[dict[str, Any]]:
        """The containers that carry a label, ended or not."""
        filters = json.dumps({"label": [label]})
        response = await self._call(
            "GET", "/containers/json", params={"all": "1", "filters": filters}, ok=(200,)
        )
        found = response.json()
        return [item for item in found if isinstance(item, dict)]


def _message_of(response: httpx.Response) -> str:
    try:
        message = response.json().get("message")
    except ValueError:
        message = None
    return str(message or response.reason_phrase)[:200]
