"""Stand-ins for what the proxy is between (a run and the hosts it asks for), so that its tests need
no network: a server that is every host, a resolver that says where names are, and a proxy that is
running on a port of this machine."""

import asyncio
import base64
import contextlib
import socket
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from factotum.common import egress
from factotum.egress.proxy import Connection, EgressProxy, Limits

KEY = b"e" * 32
NOW = 1_800_000_000

OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


class Upstream:
    """A server that stands for every host a proxy is asked to reach. It keeps what it was sent and
    answers what it is told to; with ``echo`` it sends back whatever comes (a tunnel)."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.answer = OK
        self.echo = False
        self.flood = 0
        """How many bytes to send after the answer, for a test of what is too much."""

        self.silent = False
        self._server: asyncio.Server | None = None
        self._release = asyncio.Event()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._release.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self.echo:
                while chunk := await reader.read(65536):
                    writer.write(chunk)
                    await writer.drain()
                return
            head = await reader.readuntil(b"\r\n\r\n")
            body = b""
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    body = await reader.readexactly(int(line.split(b":")[1]))
            self.received.append(head + body)
            if self.silent:
                await self._release.wait()
                return
            writer.write(self.answer)
            if self.flood:
                writer.write(b"x" * self.flood)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()


class Names:
    """A resolver that knows the names it is told of, and does not know any other."""

    def __init__(self, **known: Sequence[str]) -> None:
        self.known = {name.replace("_", "."): list(addresses) for name, addresses in known.items()}
        self.asked: list[str] = []

    async def __call__(self, host: str) -> Sequence[str]:
        self.asked.append(host)
        if host not in self.known:
            raise socket.gaierror(-2, "Name or service not known")
        return self.known[host]


class Dialer:
    """Connects to the upstream whatever address it is asked for, and remembers which."""

    def __init__(self, upstream: Upstream) -> None:
        self.upstream = upstream
        self.asked: list[tuple[str, int]] = []
        self.refuse: set[str] = set()
        """Addresses that do not answer."""

        self.delay_s = 0.0

    async def __call__(self, address: str, port: int) -> Connection:
        self.asked.append((address, port))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if address in self.refuse:
            raise ConnectionRefusedError
        return await asyncio.open_connection("127.0.0.1", self.upstream.port)


def grant_header(*hosts: str, run: str = "run-1", expires_at: int = NOW + 60) -> str:
    """The ``Proxy-Authorization`` a run is given for the hosts it is granted."""
    token = egress.issue(KEY, run=run, hosts=hosts, expires_at=expires_at)
    return "Basic " + base64.b64encode(f"{egress.USER}:{token}".encode()).decode()


@dataclass
class Running:
    """A proxy that is running, with the doubles it was made of."""

    proxy: EgressProxy
    port: int
    upstream: Upstream
    names: Names
    dialer: Dialer
    now: list[float] = field(default_factory=lambda: [float(NOW)])

    async def send(self, raw: bytes, *, read: bool = True, wait_s: float = 5.0) -> bytes:
        """Everything the proxy answers to ``raw``, until it closes the connection."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(raw)
            await writer.drain()
            if not read:
                return b""
            chunks: list[bytes] = []
            try:
                async with asyncio.timeout(wait_s):
                    while chunk := await reader.read(65536):
                        chunks.append(chunk)
            except ConnectionError:
                pass  # the proxy cut the connection: what came before is what it answered
            return b"".join(chunks)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def get(self, url: str, auth: str | None, *, extra: str = "") -> bytes:
        head = f"GET {url} HTTP/1.1\r\nHost: ignored\r\n"
        if auth is not None:
            head += f"Proxy-Authorization: {auth}\r\n"
        return (head + extra + "\r\n").encode()


@contextlib.asynccontextmanager
async def running(
    *, limits: Limits | None = None, names: Names | None = None
) -> AsyncIterator[Running]:
    upstream = Upstream()
    await upstream.start()
    known = names or Names()
    dialer = Dialer(upstream)
    clock = [float(NOW)]
    proxy = EgressProxy(KEY, limits=limits, resolver=known, connector=dialer, now=lambda: clock[0])
    server = await proxy.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield Running(proxy, port, upstream, known, dialer, clock)
    finally:
        server.close()
        await server.wait_closed()
        await upstream.stop()
