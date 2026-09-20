"""A guard against server-side request forgery for URLs that users control.

Users choose the address of their model providers and search backends, so a hostile or careless
address must never turn Factotum into a way to reach things it should not: the cloud metadata
service, other services on the internal network, the host itself.

The rules, applied to every address the host name resolves to:

* Only ``http`` and ``https``, no credentials in the URL.
* Link-local, multicast, unspecified, reserved and cloud-metadata addresses are always refused.
* Private-network addresses (loopback, RFC 1918, unique-local...) are refused unless the policy
  allows them, which a deployment that reaches a server on its own network needs (an Ollama on the
  LAN, a device in the house). A deployment that must not reach into a network it shares with
  others does not allow them.
* A public address needs ``https``.
* If any resolved address is refused the whole URL is refused, which defeats a name that resolves
  to a public and a private address at once.

``GuardedTransport`` applies the rules where the connection is made and connects to the address
that was checked, keeping the original ``Host`` header and TLS server name. A name that answers
differently the second time it is resolved therefore cannot slip through. It also keeps the
connections it opens for the next request, a pool for each host name.
"""

import asyncio
import contextlib
import ipaddress
import socket
import ssl
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self, cast
from urllib.parse import urlsplit

import httpx

from factotum.common.redaction import redact_text
from factotum.contracts.errors import UrlNotAllowedError

Resolver = Callable[[str], Awaitable[Sequence[str]]]
"""Resolves a host name to the IP addresses it points to (injected in tests)."""

_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})
_ALWAYS_FORBIDDEN_NETWORKS = (
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IPv6 metadata
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class IpClass(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    FORBIDDEN = "forbidden"


def classify_ip(address: str) -> IpClass:
    """Classify an IP address. Raises ``ValueError`` when it is not one."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.is_loopback:
        # Checked first: Python also calls ::1 "reserved" because it sits in ::/8.
        return IpClass.PRIVATE
    if (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or any(ip in network for network in _ALWAYS_FORBIDDEN_NETWORKS)
    ):
        return IpClass.FORBIDDEN
    return IpClass.PUBLIC if ip.is_global else IpClass.PRIVATE


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    allow_private_networks: bool = False
    """Whether the private network (this machine, the LAN) may be reached: a matter of what the
    deployment is, not of what a user says about an address."""

    @classmethod
    def public_only(cls) -> Self:
        """The public internet only."""
        return cls(allow_private_networks=False)

    @classmethod
    def private_networks_allowed(cls) -> Self:
        """The private network too, as an on-premise deployment needs."""
        return cls(allow_private_networks=True)


async def system_resolver(host: str) -> Sequence[str]:
    """Resolve with the operating system, without blocking the event loop."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _refuse(url: str, host: str | None, reason: str) -> UrlNotAllowedError:
    return UrlNotAllowedError(
        f"URL not allowed: {reason}", details={"url": redact_text(url), "host": host}
    )


def _split(url: str) -> tuple[str, str, int]:
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise _refuse(url, None, "malformed URL") from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _refuse(url, host, f"scheme {scheme or '(none)'!r} is not http or https")
    if not host:
        raise _refuse(url, None, "no host")
    if parts.username or parts.password:
        raise _refuse(url, host, "credentials in the URL")
    return scheme, host.lower(), port or (443 if scheme == "https" else 80)


async def vet_url(
    url: str, policy: UrlPolicy, *, resolver: Resolver | None = None
) -> tuple[str, ...]:
    """Check ``url`` against ``policy`` and return the IP addresses it is safe to connect to.

    Raises ``UrlNotAllowedError`` when it is not allowed.
    """
    scheme, host, _port = _split(url)
    if host in _METADATA_HOSTS:
        raise _refuse(url, host, "cloud metadata host")

    addresses = await _addresses_of(url, host, resolver or system_resolver)

    classes: list[IpClass] = []
    for address in addresses:
        try:
            classes.append(classify_ip(address))
        except ValueError:
            raise _refuse(url, host, f"{address!r} is not an IP address") from None

    if IpClass.FORBIDDEN in classes:
        raise _refuse(url, host, "link-local, multicast, reserved or cloud-metadata address")
    if IpClass.PRIVATE in classes and not policy.allow_private_networks:
        raise _refuse(url, host, "private-network address")
    if IpClass.PUBLIC in classes and scheme != "https":
        raise _refuse(url, host, "a public address needs https")
    return tuple(addresses)


async def _addresses_of(url: str, host: str, resolver: Resolver) -> Sequence[str]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    try:
        addresses = await resolver(host)
    except OSError:
        raise _refuse(url, host, "the host name could not be resolved") from None
    if not addresses:
        raise _refuse(url, host, "the host name resolved to no address")
    return addresses


async def check_url(url: str, policy: UrlPolicy, *, resolver: Resolver | None = None) -> None:
    """Like :func:`vet_url`, for callers that only need the verdict."""
    await vet_url(url, policy, resolver=resolver)


_tls: ssl.SSLContext | None = None


def shared_tls_context() -> ssl.SSLContext:
    """The TLS settings every guarded connection uses, made once.

    Making them loads the certificate authorities and takes about 300 ms on Windows: a client made
    for each request (as the gateway does) would pay that for every call to a model, even one on the
    same machine over plain HTTP. A context holds no connection and nothing that changes, so
    connections can share it.
    """
    global _tls
    if _tls is None:
        _tls = httpx.create_ssl_context()
    return _tls


Origin = tuple[str, str, int]
"""The scheme, host name and port a request was written for: what a pool of connections is for."""

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_ORIGINS = 32
_POOL_LIMITS = httpx.Limits(
    max_connections=None, max_keepalive_connections=16, keepalive_expiry=30.0
)
"""No limit to the connections open at once (a call used to have a client, and so a pool, of its
own), and a few kept, for half a minute, for the next call."""


def _origin_of(url: httpx.URL) -> Origin:
    return (url.scheme, url.host, url.port or _DEFAULT_PORTS.get(url.scheme, 0))


def _open_pool() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(verify=shared_tls_context(), limits=_POOL_LIMITS)


async def _close(pool: httpx.AsyncBaseTransport) -> None:
    """Close a pool. One that will not close is no reason to fail a request or a shutdown."""
    with contextlib.suppress(Exception):
        await pool.aclose()


class _Lane:
    """The connections to one origin, and how many responses are still being read from them."""

    __slots__ = ("busy", "pool")

    def __init__(self, pool: httpx.AsyncBaseTransport) -> None:
        self.pool = pool
        self.busy = 0

    def release(self) -> None:
        self.busy -= 1


class _Counted(httpx.AsyncByteStream):
    """A response body that says when it is closed, so that a pool somebody is still reading from
    is never taken away."""

    def __init__(self, body: httpx.AsyncByteStream, closed: Callable[[], None]) -> None:
        self._body = body
        self._closed = closed
        self._done = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._body:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._body.aclose()
        finally:
            if not self._done:
                self._done = True
                self._closed()


class GuardedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that vets every request, redirects included, and pins the address.

    It keeps the connections it opens for the next request: a pool for each origin (scheme, host
    name and port). Two host names that resolve to the same address do not share connections, as
    each was made, and its certificate checked, for one name. The pools of the ``max_origins``
    origins used last are kept; one that somebody is still reading a response from is never closed
    to make room. ``aclose`` closes them all.

    ``inner`` stands for the network instead of the pools (a test's, or a transport of the caller's
    own), and ``open_pool`` makes a pool.
    """

    def __init__(
        self,
        policy: UrlPolicy,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        max_origins: int = _MAX_ORIGINS,
        open_pool: Callable[[], httpx.AsyncBaseTransport] = _open_pool,
    ) -> None:
        self._policy = policy
        self._inner = inner
        self._resolver = resolver
        self._max_origins = max_origins
        self._open_pool = open_pool
        self._lanes: OrderedDict[Origin, _Lane] = OrderedDict()
        if inner is None and open_pool is _open_pool:
            shared_tls_context()  # made now, and not while the first request waits for it

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        addresses = await vet_url(str(request.url), self._policy, resolver=self._resolver)
        pinned = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=addresses[0]),
            headers=request.headers,  # keeps Host as the caller wrote it
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        if self._inner is not None:
            return await self._inner.handle_async_request(pinned)
        lane = await self._borrow(_origin_of(request.url))
        try:
            response = await lane.pool.handle_async_request(pinned)
        except BaseException:
            lane.release()
            raise
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_Counted(cast(httpx.AsyncByteStream, response.stream), lane.release),
            extensions=response.extensions,
        )

    async def _borrow(self, origin: Origin) -> _Lane:
        if origin not in self._lanes:
            await self._make_room()
        lane = self._lanes.get(origin)  # somebody may have opened it while room was made
        if lane is None:
            lane = self._lanes[origin] = _Lane(self._open_pool())
        else:
            self._lanes.move_to_end(origin)
        lane.busy += 1
        return lane

    async def _make_room(self) -> None:
        while len(self._lanes) >= self._max_origins:
            idle = next((origin for origin, lane in self._lanes.items() if lane.busy == 0), None)
            if idle is None:
                return  # every pool is in use: one more than the limit, until one is free
            await _close(self._lanes.pop(idle).pool)

    async def aclose(self) -> None:
        lanes, self._lanes = list(self._lanes.values()), OrderedDict()
        if self._inner is not None:
            await self._inner.aclose()
        for lane in lanes:
            await _close(lane.pool)


class LentTransport(httpx.AsyncBaseTransport):
    """A transport lent to a client: the client uses it, and closing the client leaves it open.

    Whoever owns the transport closes it. Many short-lived clients, each with the timeout of its own
    call and a cookie jar of its own, can then share the connections of one pool.
    """

    def __init__(self, lender: httpx.AsyncBaseTransport) -> None:
        self._lender = lender

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._lender.handle_async_request(request)

    async def aclose(self) -> None:
        """Nothing: what was lent is not the borrower's to close."""


def guarded_client(
    policy: UrlPolicy,
    *,
    resolver: Resolver | None = None,
    inner: httpx.AsyncBaseTransport | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that cannot reach anything ``policy`` refuses.

    Redirects are not followed unless the caller asks for it; when they are, every hop is vetted.
    """
    client_kwargs.setdefault("follow_redirects", False)
    transport = GuardedTransport(policy, inner=inner, resolver=resolver)
    return httpx.AsyncClient(transport=transport, **client_kwargs)
