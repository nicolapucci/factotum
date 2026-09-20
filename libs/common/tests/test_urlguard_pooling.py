"""Connections kept for the next request: a pool for each origin, lent to the clients of a call."""

from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from factotum.common.testing import KeepAliveServer
from factotum.common.urlguard import GuardedTransport, LentTransport, Resolver, UrlPolicy

PRIVATE_OK = UrlPolicy.private_networks_allowed()


def resolver_for(table: dict[str, list[str]]) -> Resolver:
    async def resolve(host: str) -> Sequence[str]:
        if host not in table:
            raise OSError(f"unknown host {host}")
        return table[host]

    return resolve


HOSTS = resolver_for({name: ["127.0.0.1"] for name in ("a.test", "b.test", "c.test")})
"""Every name is the machine itself: several names, one address."""


async def get(transport: httpx.AsyncBaseTransport, url: str) -> httpx.Response:
    """A call as the gateway makes it: a client of its own on a lent transport, closed after."""
    async with httpx.AsyncClient(transport=LentTransport(transport)) as client:
        return await client.get(url)


@pytest.fixture
async def server() -> AsyncIterator[KeepAliveServer]:
    async with KeepAliveServer() as running:
        yield running


@pytest.fixture
async def transport(server: KeepAliveServer) -> AsyncIterator[GuardedTransport]:
    guarded = GuardedTransport(PRIVATE_OK, resolver=HOSTS)
    yield guarded
    await guarded.aclose()


class TestConnectionsAreKept:
    async def test_requests_to_one_origin_share_a_connection(
        self, server: KeepAliveServer, transport: GuardedTransport
    ) -> None:
        for _ in range(3):
            assert (await get(transport, server.url("a.test"))).text == "ok"

        assert (server.connections, len(server.requests)) == (1, 3)

    async def test_the_connection_outlives_the_client_that_made_it(
        self, server: KeepAliveServer, transport: GuardedTransport
    ) -> None:
        await get(transport, server.url("a.test"))  # the client is closed when this returns

        assert server.open == 1

    async def test_a_host_name_has_connections_of_its_own_even_at_the_same_address(
        self, server: KeepAliveServer, transport: GuardedTransport
    ) -> None:
        # Each connection was made, and its certificate checked, for one name.
        for host in ("a.test", "b.test", "a.test", "b.test"):
            await get(transport, server.url(host))

        assert server.connections == 2

    async def test_a_body_read_in_pieces_gives_the_connection_back(
        self, server: KeepAliveServer, transport: GuardedTransport
    ) -> None:
        async with httpx.AsyncClient(transport=LentTransport(transport)) as client:
            async with client.stream("GET", server.url("a.test")) as response:
                assert [chunk async for chunk in response.aiter_bytes()] == [b"ok"]
            await client.get(server.url("a.test"))

        assert server.connections == 1

    async def test_closing_the_transport_closes_the_connections(
        self, server: KeepAliveServer, transport: GuardedTransport
    ) -> None:
        await get(transport, server.url("a.test"))

        await transport.aclose()

        await server.until_closed()
        assert server.open == 0

    async def test_it_connects_to_the_vetted_address_and_keeps_the_host_and_the_tls_name(
        self, server: KeepAliveServer, transport: GuardedTransport
    ) -> None:
        await get(transport, server.url("a.test"))

        assert server.requests[0]["host"] == f"a.test:{server.port}"


class Pool(httpx.AsyncBaseTransport):
    """The connections to one origin, as a test sees them: it answers, and remembers."""

    def __init__(self, *, fails: bool = False, stubborn: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.closed = False
        self._fails = fails
        self._stubborn = stubborn

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._fails:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(
            201,
            headers={"x-test": "yes"},
            stream=Body(b"hel", b"lo"),
            extensions={"reason_phrase": b"Fine"},
        )

    async def aclose(self) -> None:
        if self._stubborn:
            raise RuntimeError("will not close")
        self.closed = True


class Body(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class Pools:
    """What a transport under test makes its pools with: they are kept, in the order made. One of
    them (counting from 0) can be told to fail its requests, or to refuse to close."""

    def __init__(self, *, fails: int | None = None, stubborn: int | None = None) -> None:
        self.made: list[Pool] = []
        self._fails = fails
        self._stubborn = stubborn

    def __call__(self) -> Pool:
        number = len(self.made)
        pool = Pool(fails=number == self._fails, stubborn=number == self._stubborn)
        self.made.append(pool)
        return pool


def request(host: str, *, scheme: str = "http") -> httpx.Request:
    return httpx.Request("GET", f"{scheme}://{host}/")


async def fetch(transport: GuardedTransport, host: str) -> None:
    """A request whose answer is read to the end and closed."""
    response = await transport.handle_async_request(request(host))
    await response.aread()
    await response.aclose()


def limited(pools: Pools, origins: int) -> GuardedTransport:
    return GuardedTransport(PRIVATE_OK, resolver=HOSTS, max_origins=origins, open_pool=pools)


class TestHowManyPoolsAreKept:
    async def test_the_pool_used_least_recently_is_closed_to_make_room(self) -> None:
        pools = Pools()
        transport = limited(pools, 2)

        for host in ("a.test", "b.test", "a.test", "c.test"):
            await fetch(transport, host)

        a, b, c = pools.made
        assert (a.closed, b.closed, c.closed) == (False, True, False)
        assert len(a.requests) == 2  # the pool of "a" was used again, not made again

    async def test_a_pool_somebody_is_reading_from_is_not_closed_to_make_room(self) -> None:
        pools = Pools()
        transport = limited(pools, 1)
        reading = await transport.handle_async_request(request("a.test"))  # neither read nor closed

        await fetch(transport, "b.test")  # no pool is free to close: one more than the limit

        a, b = pools.made
        assert (a.closed, b.closed) == (False, False)

        await reading.aclose()
        await fetch(transport, "c.test")  # now two are free, and both make room

        assert (a.closed, b.closed) == (True, True)
        assert len(pools.made) == 3

    async def test_a_request_that_fails_gives_its_pool_back(self) -> None:
        pools = Pools(fails=0)
        transport = limited(pools, 1)
        with pytest.raises(httpx.ConnectError):
            await fetch(transport, "a.test")

        await fetch(transport, "b.test")

        assert pools.made[0].closed  # it was not thought still in use

    async def test_closing_a_body_twice_does_not_free_a_pool_somebody_else_is_using(self) -> None:
        pools = Pools()
        transport = limited(pools, 1)
        first = await transport.handle_async_request(request("a.test"))
        second = await transport.handle_async_request(request("a.test"))

        await first.stream.aclose()  # type: ignore[union-attr]
        await first.stream.aclose()  # type: ignore[union-attr]
        await fetch(transport, "b.test")

        assert pools.made[0].closed is False  # the second answer is still being read
        await second.aclose()

    async def test_the_same_name_over_https_is_another_origin(self) -> None:
        pools = Pools()
        transport = limited(pools, 8)

        for scheme in ("http", "https", "http"):
            response = await transport.handle_async_request(request("a.test", scheme=scheme))
            await response.aclose()

        assert len(pools.made) == 2
        assert len(pools.made[0].requests) == 2

    async def test_closing_the_transport_closes_every_pool(self) -> None:
        pools = Pools()
        transport = limited(pools, 8)
        for host in ("a.test", "b.test"):
            await fetch(transport, host)

        await transport.aclose()

        assert [pool.closed for pool in pools.made] == [True, True]

    async def test_a_pool_that_will_not_close_does_not_stop_the_others_or_a_request(self) -> None:
        pools = Pools(stubborn=0)
        transport = limited(pools, 1)
        await fetch(transport, "a.test")

        await fetch(transport, "b.test")  # it needs the room the stubborn pool takes

        await transport.aclose()
        assert pools.made[1].closed

    async def test_the_answer_comes_through_as_it_was(self) -> None:
        pools = Pools()
        transport = limited(pools, 8)

        response = await transport.handle_async_request(request("a.test"))

        assert response.status_code == 201
        assert response.headers["x-test"] == "yes"
        assert response.extensions["reason_phrase"] == b"Fine"
        assert await response.aread() == b"hello"

    async def test_the_pool_is_given_the_vetted_address_with_the_host_and_tls_name_kept(
        self,
    ) -> None:
        pools = Pools()
        transport = limited(pools, 8)

        await fetch(transport, "a.test")

        (sent,) = pools.made[0].requests
        assert sent.url.host == "127.0.0.1"
        assert sent.headers["host"] == "a.test"
        assert sent.extensions["sni_hostname"] == "a.test"


class TestALentTransport:
    async def test_closing_the_client_leaves_it_open(self) -> None:
        lender = Pool()

        response = await get(lender, "http://a.test/")

        assert response.headers["x-test"] == "yes"
        assert (len(lender.requests), lender.closed) == (1, False)

    async def test_it_does_not_close_what_it_lent_even_when_asked_to(self) -> None:
        lender = Pool()

        await LentTransport(lender).aclose()

        assert lender.closed is False
