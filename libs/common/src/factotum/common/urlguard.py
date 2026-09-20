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
differently the second time it is resolved therefore cannot slip through.
"""

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self
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


class GuardedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that vets every request, redirects included, and pins the address."""

    def __init__(
        self,
        policy: UrlPolicy,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._policy = policy
        self._inner = (
            inner if inner is not None else httpx.AsyncHTTPTransport(verify=shared_tls_context())
        )
        self._resolver = resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        addresses = await vet_url(str(request.url), self._policy, resolver=self._resolver)
        pinned = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=addresses[0]),
            headers=request.headers,  # keeps Host as the caller wrote it
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


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
