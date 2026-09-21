"""The egress proxy: the only way out of the sandbox of a skill.

A skill runs on a network that has nothing on it but this proxy, and it is given a grant, signed by
the runner, that says which hosts it may reach (``factotum.common.egress``). It speaks to the proxy
the way any program speaks to a proxy (``HTTP_PROXY``): a request for a URL, or a ``CONNECT`` for a
tunnel to a host that is spoken to in a language the proxy does not need to understand (https).

For every request the proxy checks, in this order: that the grant is one that the runner made and
that has not run out; that the host is one it lists; that *every* address the host is resolved to is
one that this run may reach (``policy``); and only then does it connect, to the address it checked,
so that a name that answers differently a second time gets nothing. What goes through is limited in
time, in bytes and in how long a connection may be silent.

It speaks no more of HTTP than it must: a request in the clear is sent on as it is with its
connection closed after it (nothing is kept alive, nothing is chunked), and what a host answers is
sent back as it comes. A request that is not plainly one is refused.
"""

import asyncio
import base64
import binascii
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from factotum.common import egress
from factotum.common.urlguard import Resolver, system_resolver
from factotum.contracts.errors import EgressGrantError
from factotum.egress.policy import is_address, match_host, normalize_host, refusal_of_address

logger = logging.getLogger("factotum.egress")

Connection = tuple[asyncio.StreamReader, asyncio.StreamWriter]
Connector = Callable[[str, int], Awaitable[Connection]]
"""Opens a connection to an address that was checked (injected in tests)."""

_METHOD = re.compile(r"^[A-Z]{1,16}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authorization", "proxy-authenticate", "proxy-connection"}
    | {"te", "trailer", "transfer-encoding", "upgrade", "host", "content-length", "expect"}
)
"""What is not sent on to the host: what is between the client and the proxy, and what the proxy
says itself."""

_CHUNK = 65536


async def open_connection(address: str, port: int) -> Connection:
    return await asyncio.open_connection(address, port)


@dataclass(frozen=True, slots=True)
class Limits:
    max_connections: int = 64
    header_timeout_s: float = 10.0
    connect_timeout_s: float = 10.0
    idle_timeout_s: float = 30.0
    max_duration_s: float = 130.0
    max_bytes: int = 20_000_000
    max_head_bytes: int = 16_384
    connect_ports: frozenset[int] = field(default_factory=lambda: frozenset({443}))


class Refusal(Exception):  # noqa: N818 - it says what the proxy did, not what failed
    def __init__(
        self,
        status: int,
        reason: str,
        text: str,
        *,
        headers: Sequence[tuple[str, str]] = (),
        why: str | None = None,
    ) -> None:
        super().__init__(text)
        self.status = status
        self.reason = reason
        self.text = text
        self.headers = tuple(headers)
        self.why = why or text


class Cut(Exception):  # noqa: N818
    """A connection that went on for too long, said nothing for too long or carried too much."""


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    target: str
    version: str
    headers: tuple[tuple[str, str], ...]

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), None)

    def all(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]


def _is_number(text: str) -> bool:
    """Digits that ``int`` reads: ``str.isdigit`` also says yes to a superscript two."""
    return text.isascii() and text.isdigit()


def bad_request(why: str) -> Refusal:
    return Refusal(400, "Bad Request", "that is not a request the proxy understands", why=why)


def parse_head(raw: bytes) -> Request:
    """The request that ``raw`` (everything up to the blank line) says. A request that is not
    plainly one is refused: the proxy is not the place to be liberal about what is sent on."""
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise bad_request("the request line")
    method, target, version = parts
    if not _METHOD.fullmatch(method) or not target:
        raise bad_request("the request line")
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise bad_request("the version")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            break
        if line[0] in " \t":
            raise bad_request("a header that goes on to another line")
        name, colon, value = line.partition(":")
        value = value.strip(" \t")
        if not colon or not _HEADER_NAME.fullmatch(name):
            raise bad_request("a header")
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
            raise bad_request("a header with a control character")
        headers.append((name, value))
    return Request(method, target, version, tuple(headers))


def parse_authority(target: str) -> tuple[str, int]:
    """The host and port of ``CONNECT host:port``."""
    host, colon, port = target.rpartition(":")
    if not colon or not host or not _is_number(port) or not 1 <= int(port) <= 65535:
        raise bad_request("the host to connect to")
    if host.startswith("[") or "@" in host or "/" in host:
        raise Refusal(403, "Forbidden", "the proxy does not connect to that", why="an odd host")
    return normalize_host(host), int(port)


def parse_absolute(target: str) -> tuple[str, int, str]:
    """The host, the port and what to ask for, of a request for ``http://host[:port]/path``."""
    try:
        parts = urlsplit(target)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise bad_request("the address asked for") from None
    if parts.scheme.lower() != "http" or not host:
        raise bad_request("the address asked for")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Refusal(403, "Forbidden", "the proxy does not connect to that", why="credentials")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return normalize_host(host), port or 80, path


def basic_credentials(request: Request) -> str:
    """The password of the ``Proxy-Authorization`` of a request, which is the grant."""
    challenge = ("Proxy-Authenticate", 'Basic realm="egress"')
    unauthorized = Refusal(
        407,
        "Proxy Authentication Required",
        "the proxy needs the grant of a run",
        headers=[challenge],
    )
    value = request.header("Proxy-Authorization")
    if value is None:
        raise unauthorized
    scheme, _, credentials = value.partition(" ")
    if scheme.lower() != "basic":
        raise unauthorized
    try:
        user, _, password = (
            base64.b64decode(credentials.strip(), validate=True).decode("ascii").partition(":")
        )
    except (binascii.Error, ValueError):
        raise unauthorized from None
    if user != egress.USER:
        raise unauthorized
    return password


def body_length(request: Request, limits: Limits) -> int:
    """How many bytes of body a request says it has. A body that has no plain length is refused."""
    if request.all("Transfer-Encoding"):
        raise Refusal(
            501, "Not Implemented", "the proxy sends on requests with a length", why="chunked"
        )
    if request.header("Upgrade") is not None:
        raise Refusal(501, "Not Implemented", "the proxy does not upgrade", why="upgrade")
    lengths = set(request.all("Content-Length"))
    if not lengths:
        return 0
    if len(lengths) != 1:
        raise bad_request("more than one length")
    (length,) = lengths
    if not _is_number(length) or int(length) > limits.max_bytes:
        raise Refusal(413, "Payload Too Large", "that is too much to send", why="length")
    return int(length)


def forwarded_head(request: Request, host: str, port: int, path: str, length: int) -> bytes:
    """The request as the host is sent it: what the proxy and the client say to each other is not
    passed on, and the connection is closed after the answer."""
    authority = host if port == 80 else f"{host}:{port}"
    lines = [f"{request.method} {path} HTTP/1.1", f"Host: {authority}"]
    lines += [
        f"{name}: {value}" for name, value in request.headers if name.lower() not in _HOP_BY_HOP
    ]
    if length or request.method in {"POST", "PUT", "PATCH"}:
        lines.append(f"Content-Length: {length}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class Meter:
    """How much has gone through a connection, and until when it may go on."""

    def __init__(self, limits: Limits) -> None:
        self._limit = limits.max_bytes
        self.count = 0

    def add(self, size: int) -> None:
        self.count += size
        if self.count > self._limit:
            raise Cut("too much went through")


class EgressProxy:
    def __init__(
        self,
        key: bytes,
        *,
        limits: Limits | None = None,
        resolver: Resolver = system_resolver,
        connector: Connector = open_connection,
        now: Callable[[], float] = time.time,
    ) -> None:
        if len(key) < egress.MIN_KEY_BYTES:
            raise ValueError(f"the key of the egress is at least {egress.MIN_KEY_BYTES} bytes")
        self._key = key
        self._limits = limits or Limits()
        self._resolver = resolver
        self._connector = connector
        self._now = now
        self._slots = asyncio.Semaphore(self._limits.max_connections)

    @property
    def limits(self) -> Limits:
        return self._limits

    async def serve(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle, host, port, limit=self._limits.max_head_bytes
        )

    # ---- one client ---------------------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self._slots.locked():
                raise Refusal(
                    503, "Service Unavailable", "the proxy is busy", headers=[("Retry-After", "1")]
                )
            async with self._slots:
                await self._serve(reader, writer)
        except Refusal as refusal:
            await self._refuse(reader, writer, refusal)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError, Cut):
            pass  # the other side went away, or was cut
        except Exception:
            logger.exception("the proxy failed")
            await self._refuse(
                reader, writer, Refusal(500, "Internal Server Error", "the proxy failed")
            )
        finally:
            await self._close(writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = parse_head(await self._head(reader))
        grant = self._grant(request)
        if request.method == "CONNECT":
            await self._tunnel(grant, request, reader, writer)
        else:
            await self._forward(grant, request, reader, writer)

    async def _head(self, reader: asyncio.StreamReader) -> bytes:
        try:
            return await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), self._limits.header_timeout_s
            )
        except asyncio.LimitOverrunError:
            raise Refusal(
                431, "Request Header Fields Too Large", "that request has too much in its header"
            ) from None
        except TimeoutError:
            raise Refusal(408, "Request Timeout", "the request did not come") from None

    def _grant(self, request: Request) -> egress.Grant:
        try:
            return egress.verify(self._key, basic_credentials(request), now=int(self._now()))
        except EgressGrantError:
            logger.info("egress refused a request whose grant is not good")
            raise Refusal(
                407,
                "Proxy Authentication Required",
                "the proxy needs the grant of a run",
                headers=[("Proxy-Authenticate", 'Basic realm="egress"')],
                why="the grant is not good",
            ) from None

    # ---- where it may go ----------------------------------------------------------------------

    async def _addresses(self, grant: egress.Grant, host: str, port: int, via: str) -> list[str]:
        """The addresses of a host that this run may connect to. Raises a refusal, and says why in
        the log, when there is one that it may not: every address is checked, so that a name that
        answers with a good one and a bad one gets neither."""
        match = match_host(grant.hosts, host)
        if match is None:
            self._log(grant, host, port, via, "refused", "the grant does not list the host")
            raise Refusal(403, "Forbidden", "the grant of this run does not list that host")
        try:
            addresses = [host] if is_address(host) else list(await self._resolver(host))
        except OSError:
            self._log(grant, host, port, via, "failed", "the name did not resolve")
            raise Refusal(502, "Bad Gateway", "that host could not be found") from None
        if not addresses:
            raise Refusal(502, "Bad Gateway", "that host could not be found")
        for address in addresses:
            reason = refusal_of_address(address, match)
            if reason is not None:
                self._log(grant, host, port, via, "refused", f"an address of it: {reason}")
                raise Refusal(403, "Forbidden", "the grant of this run does not reach that host")
        return addresses

    async def _dial(self, addresses: Sequence[str], port: int) -> Connection:
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    self._connector(address, port), self._limits.connect_timeout_s
                )
            except (OSError, TimeoutError):
                continue
        raise Refusal(502, "Bad Gateway", "that host did not answer")

    # ---- a tunnel -----------------------------------------------------------------------------

    async def _tunnel(
        self,
        grant: egress.Grant,
        request: Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        host, port = parse_authority(request.target)
        if port not in self._limits.connect_ports:
            self._log(grant, host, port, "connect", "refused", "the port is not one for a tunnel")
            raise Refusal(403, "Forbidden", "the proxy does not make a tunnel to that port")
        addresses = await self._addresses(grant, host, port, "connect")
        upstream = await self._dial(addresses, port)
        self._log(grant, host, port, "connect", "allowed", None)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._pipe((reader, writer), upstream)

    async def _pipe(self, client: Connection, upstream: Connection) -> None:
        meter = Meter(self._limits)
        try:
            async with asyncio.timeout(self._limits.max_duration_s), asyncio.TaskGroup() as group:
                group.create_task(self._copy(client[0], upstream[1], meter))
                group.create_task(self._copy(upstream[0], client[1], meter))
        except* (Cut, OSError):
            pass  # too much, too long, or a side that went away (with a reset, most often)
        finally:
            await self._close(upstream[1])

    async def _copy(
        self, source: asyncio.StreamReader, sink: asyncio.StreamWriter, meter: Meter
    ) -> None:
        """Everything that comes from one side goes to the other, until the one that sends says
        it is done (and then the other is told), or the connection is too much."""
        while True:
            try:
                chunk = await asyncio.wait_for(source.read(_CHUNK), self._limits.idle_timeout_s)
            except TimeoutError:
                raise Cut("silent for too long") from None
            if not chunk:
                with contextlib.suppress(OSError, RuntimeError):
                    if sink.can_write_eof():
                        sink.write_eof()
                return
            meter.add(len(chunk))
            sink.write(chunk)
            await sink.drain()

    # ---- a request in the clear ---------------------------------------------------------------

    async def _forward(
        self,
        grant: egress.Grant,
        request: Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        host, port, path = parse_absolute(request.target)
        length = body_length(request, self._limits)
        addresses = await self._addresses(grant, host, port, "http")
        upstream_reader, upstream_writer = await self._dial(addresses, port)
        self._log(grant, host, port, "http", "allowed", None)
        meter = Meter(self._limits)
        try:
            async with asyncio.timeout(self._limits.max_duration_s):
                upstream_writer.write(forwarded_head(request, host, port, path, length))
                if length:
                    await self._send_body(reader, upstream_writer, length, meter)
                await upstream_writer.drain()
                await self._copy(upstream_reader, writer, meter)
        finally:
            await self._close(upstream_writer)

    async def _send_body(
        self,
        reader: asyncio.StreamReader,
        upstream: asyncio.StreamWriter,
        length: int,
        meter: Meter,
    ) -> None:
        left = length
        while left:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(min(_CHUNK, left)), self._limits.idle_timeout_s
                )
            except TimeoutError:
                raise Cut("silent for too long") from None
            if not chunk:
                raise Cut("the body did not come")
            meter.add(len(chunk))
            upstream.write(chunk)
            left -= len(chunk)
            await upstream.drain()

    # ---- what is said back --------------------------------------------------------------------

    async def _refuse(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, refusal: Refusal
    ) -> None:
        """Say no, and let the answer be read: a connection that is closed with what the client is
        still sending unread is reset, and a reset can take the answer with it. So the proxy says
        it is done, and reads what is still coming (for a moment, and without looking at it)."""
        body = (refusal.text + "\n").encode("utf-8")
        head = [
            f"HTTP/1.1 {refusal.status} {refusal.reason}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Connection: close",
            *[f"{name}: {value}" for name, value in refusal.headers],
        ]
        with contextlib.suppress(ConnectionError, RuntimeError, OSError, TimeoutError):
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body)
            await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
            async with asyncio.timeout(1.0):
                while await reader.read(_CHUNK):
                    pass

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, RuntimeError, OSError, TimeoutError):
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2)

    @staticmethod
    def _log(
        grant: egress.Grant, host: str, port: int, via: str, outcome: str, reason: str | None
    ) -> None:
        logger.info(
            "egress %s",
            outcome,
            extra={"run": grant.run, "target": host, "port": port, "via": via, "reason": reason},
        )
