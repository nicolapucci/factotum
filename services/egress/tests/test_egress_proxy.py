"""The proxy over real sockets on this machine, standing between a run and a server that is every
host: what it lets through, what it refuses, and what it does not let go on for too long."""

import asyncio
import base64
import logging
import socket
import struct
import urllib.request

import pytest

from factotum.egress.proxy import EgressProxy, Limits
from factotum.egress.testing import NOW, Names, grant_header, running

PUBLIC = "93.184.216.34"
LAN = "192.168.1.20"


def connect(target: str, auth: str) -> bytes:
    return f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode()


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def body_of(response: bytes) -> bytes:
    return response.split(b"\r\n\r\n", 1)[1]


class TestARequestInTheClear:
    async def test_a_host_that_is_listed_is_reached_and_what_it_answers_comes_back(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            auth = grant_header("api.example.org")

            response = await proxy.send(proxy.get("http://api.example.org/path?x=1", auth))

        assert response == b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        assert proxy.dialer.asked == [(PUBLIC, 80)]

    async def test_it_is_connected_to_the_address_that_was_checked_and_never_to_the_name(
        self,
    ) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            await proxy.send(proxy.get("http://api.example.org/", grant_header("api.example.org")))

        assert proxy.names.asked == ["api.example.org"]
        assert [address for address, _ in proxy.dialer.asked] == [PUBLIC]

    async def test_the_host_is_sent_the_request_without_what_is_between_the_run_and_the_proxy(
        self,
    ) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            auth = grant_header("api.example.org")
            raw = proxy.get(
                "http://api.example.org:8123/api/x?y=1",
                auth,
                extra="Authorization: Bearer token\r\nProxy-Connection: keep-alive\r\n",
            )

            await proxy.send(raw)

        [received] = proxy.upstream.received
        text = received.decode()
        assert text.startswith("GET /api/x?y=1 HTTP/1.1\r\nHost: api.example.org:8123\r\n")
        assert "Authorization: Bearer token\r\n" in text
        assert "Proxy-Authorization" not in text
        assert "Proxy-Connection" not in text
        assert "ignored" not in text
        assert text.endswith("Connection: close\r\n\r\n")
        assert proxy.dialer.asked == [(PUBLIC, 8123)]

    async def test_a_body_goes_on_as_long_as_it_was_said_to_be(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            raw = (
                b"POST http://api.example.org/api HTTP/1.1\r\n"
                + f"Proxy-Authorization: {grant_header('api.example.org')}\r\n".encode()
                + b"Content-Type: application/json\r\nContent-Length: 13\r\n\r\n"
                + b'{"a": "body"}EXTRA THAT IS NOT PART OF IT'
            )

            await proxy.send(raw)

        [received] = proxy.upstream.received
        assert received.endswith(b"\r\n\r\n" + b'{"a": "body"}'[:13])
        assert b"EXTRA" not in received

    async def test_what_a_host_answers_comes_back_however_big_and_whatever_it_is(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            body = bytes(range(256)) * 1000
            proxy.upstream.answer = (
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body)
            ) + body

            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert body_of(response) == body

    async def test_an_error_of_the_host_is_the_answer_too(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            proxy.upstream.answer = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"

            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert status_of(response) == 404

    async def test_a_domain_that_is_listed_lets_through_what_is_in_it(self) -> None:
        async with running(names=Names(a_b_example_org=[PUBLIC])) as proxy:
            response = await proxy.send(
                proxy.get("http://a.b.example.org/", grant_header("*.example.org"))
            )

        assert status_of(response) == 200

    async def test_an_address_that_is_listed_is_reached(self) -> None:
        async with running() as proxy:
            response = await proxy.send(proxy.get(f"http://{LAN}:8123/api", grant_header(LAN)))

        assert status_of(response) == 200
        assert proxy.dialer.asked == [(LAN, 8123)]
        assert proxy.names.asked == []

    async def test_a_name_that_answers_with_several_addresses_is_tried_in_turn(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC, "93.184.216.35"])) as proxy:
            proxy.dialer.refuse.add(PUBLIC)

            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert status_of(response) == 200
        assert proxy.dialer.asked == [(PUBLIC, 80), ("93.184.216.35", 80)]

    async def test_a_program_that_speaks_to_a_proxy_the_way_programs_do(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            token = grant_header("api.example.org").removeprefix("Basic ")

            password = base64.b64decode(token).decode().split(":", 1)[1]
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(
                    {"http": f"http://skill:{password}@127.0.0.1:{proxy.port}"}
                )
            )

            def fetch() -> tuple[int, bytes]:
                with opener.open("http://api.example.org/hello", timeout=5) as response:
                    return response.status, response.read()

            status, body = await asyncio.to_thread(fetch)

        assert (status, body) == (200, b"ok")


class Peer:
    """One end of a connection, made of what the proxy asks of a reader and a writer. ``fate`` is
    what reading from it does: nothing, for good (``None``), or failing with what it is given."""

    def __init__(self, fate: Exception | None = None) -> None:
        self.fate = fate
        self.written = b""
        self.closed = False

    async def read(self, size: int) -> bytes:
        if self.fate is not None:
            raise self.fate
        await asyncio.Event().wait()
        return b""  # pragma: no cover - it never says anything

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class TestAConnectionThatIsBrokenOff:
    """A side that is done with a connection may break it off (a reset) and not end it. The event
    loop of Linux says so to a read; that of Windows says it as an end, so what is asked here is the
    same on both: whatever a side does, the tunnel ends without an error, and the run is not sent
    what is not an answer."""

    @pytest.mark.parametrize(
        "error",
        [
            ConnectionResetError(104, "Connection reset by peer"),
            BrokenPipeError(32, "Broken pipe"),
            ConnectionAbortedError(103, "Software caused connection abort"),
            OSError(110, "Connection timed out"),
        ],
        ids=lambda error: type(error).__name__,
    )
    @pytest.mark.parametrize("side", ["host", "run"])
    async def test_the_tunnel_ends_without_an_error_and_without_a_word(
        self, error: OSError, side: str
    ) -> None:
        proxy = EgressProxy(b"e" * 32)
        run, host = Peer(error if side == "run" else None), Peer(error if side == "host" else None)

        await asyncio.wait_for(
            proxy._pipe((run, run), (host, host)),  # type: ignore[arg-type]
            5,
        )

        assert host.closed
        assert (run.written, host.written) == (b"", b"")


class TestATunnel:
    async def test_a_host_that_is_listed_is_reached_and_what_goes_through_is_not_read(
        self,
    ) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            proxy.upstream.echo = True
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            auth = grant_header("api.example.org")
            writer.write(connect("api.example.org:443", auth))
            await writer.drain()

            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            sent = b"\x16\x03\x01 whatever is in a tunnel"
            writer.write(sent)
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(sent)), 5)
            writer.close()

        assert head == b"HTTP/1.1 200 Connection Established\r\n\r\n"
        assert echoed == sent
        assert proxy.dialer.asked == [(PUBLIC, 443)]

    async def test_a_tunnel_ends_when_the_run_says_it_is_done(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            proxy.upstream.echo = True
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            auth = grant_header("api.example.org")
            writer.write(connect("api.example.org:443", auth))
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"last words")
            writer.write_eof()

            rest = await asyncio.wait_for(reader.read(), 5)
            writer.close()

        assert rest == b"last words"

    async def test_a_host_that_breaks_the_connection_off_ends_it_without_a_word(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="factotum.egress")
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            proxy.upstream.echo = True
            proxy.upstream.reset = True
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(connect("api.example.org:443", grant_header("api.example.org")))
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"the last thing that is said")
            await writer.drain()

            try:
                rest = await asyncio.wait_for(reader.read(), 5)
            except ConnectionError:
                rest = b""
            writer.close()

        assert rest == b""
        assert "the proxy failed" not in caplog.text
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    async def test_a_run_that_breaks_the_connection_off_ends_it_without_a_word(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="factotum.egress")
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            proxy.upstream.echo = True
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(connect("api.example.org:443", grant_header("api.example.org")))
            await reader.readuntil(b"\r\n\r\n")
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            writer.close()
            await asyncio.sleep(0.2)

        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    async def test_a_port_that_is_not_one_for_a_tunnel_is_refused(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            auth = grant_header("api.example.org")

            response = await proxy.send(connect("api.example.org:22", auth))

        assert status_of(response) == 403
        assert proxy.dialer.asked == []

    async def test_the_ports_of_a_tunnel_are_the_ones_it_is_set_up_with(self) -> None:
        limits = Limits(connect_ports=frozenset({8443}))
        async with running(names=Names(api_example_org=[PUBLIC]), limits=limits) as proxy:
            auth = grant_header("api.example.org")
            refused = await proxy.send(connect("api.example.org:443", auth))
            proxy.upstream.echo = True
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(connect("api.example.org:8443", auth))
            allowed = await reader.readuntil(b"\r\n\r\n")
            writer.close()

        assert status_of(refused) == 403
        assert allowed.startswith(b"HTTP/1.1 200")

    async def test_a_host_that_is_not_listed_gets_no_tunnel(self) -> None:
        async with running(names=Names(evil_org=[PUBLIC])) as proxy:
            auth = grant_header("api.example.org")

            response = await proxy.send(connect("evil.org:443", auth))

        assert status_of(response) == 403
        assert proxy.dialer.asked == []


class TestWithoutAGoodGrant:
    async def test_a_request_with_none_is_asked_for_one(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            response = await proxy.send(proxy.get("http://api.example.org/", None))

        assert status_of(response) == 407
        assert b'Proxy-Authenticate: Basic realm="egress"' in response
        assert proxy.dialer.asked == []
        assert proxy.names.asked == []

    @pytest.mark.parametrize("what", ["altered", "another key", "expired", "not a grant"])
    async def test_one_that_is_not_good_is_not_taken(self, what: str) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            good = grant_header("api.example.org")
            auth = {
                "altered": good[:-2] + ("AA" if not good.endswith("AA") else "BB"),
                "another key": good,  # replaced below: the proxy is given another key
                "expired": grant_header("api.example.org", expires_at=NOW - 1),
                "not a grant": "Basic c2tpbGw6bm9wZQ==",
            }[what]
            if what == "another key":
                proxy.proxy._key = b"another key that is long enough!!"

            response = await proxy.send(proxy.get("http://api.example.org/", auth))

        assert status_of(response) == 407
        assert proxy.dialer.asked == []
        assert proxy.names.asked == []

    async def test_a_grant_is_good_until_it_runs_out_by_the_clock_of_the_proxy(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            auth = grant_header("api.example.org", expires_at=NOW + 10)
            before = await proxy.send(proxy.get("http://api.example.org/", auth))
            proxy.now[0] = NOW + 11
            after = await proxy.send(proxy.get("http://api.example.org/", auth))

        assert (status_of(before), status_of(after)) == (200, 407)

    async def test_a_grant_that_is_no_good_is_said_in_the_log_without_saying_what_it_was(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with running() as proxy:
            with caplog.at_level(logging.INFO, logger="factotum.egress"):
                await proxy.send(proxy.get("http://a.org/", "Basic c2tpbGw6c2VjcmV0LXRva2Vu"))

        assert "grant is not good" in caplog.text
        assert "secret-token" not in caplog.text


class TestWhereARunMayNotGo:
    async def test_a_host_the_grant_does_not_list_is_refused_and_not_even_looked_up(self) -> None:
        async with running(names=Names(evil_org=[PUBLIC])) as proxy:
            response = await proxy.send(
                proxy.get("http://evil.org/", grant_header("api.example.org"))
            )

        assert status_of(response) == 403
        assert proxy.names.asked == []
        assert proxy.dialer.asked == []

    async def test_a_run_with_no_hosts_reaches_nothing(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            response = await proxy.send(proxy.get("http://api.example.org/", grant_header()))

        assert status_of(response) == 403

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "::1",
            "::ffff:127.0.0.1",
            "0.0.0.0",  # noqa: S104 - an address that is refused
            "169.254.169.254",
            "fe80::1",
            "224.0.0.1",
        ],
    )
    async def test_this_machine_and_what_is_not_a_place_are_refused_even_when_the_name_is_listed(
        self, address: str
    ) -> None:
        async with running(names=Names(api_example_org=[address])) as proxy:
            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert status_of(response) == 403
        assert proxy.dialer.asked == []

    async def test_a_private_address_is_reached_only_by_a_name_the_grant_gives_whole(self) -> None:
        async with running(names=Names(lamp_lan=["10.0.0.5"], other_lan=["10.0.0.6"])) as proxy:
            whole = await proxy.send(proxy.get("http://lamp.lan/", grant_header("lamp.lan")))
            wild = await proxy.send(proxy.get("http://other.lan/", grant_header("*.lan")))

        assert (status_of(whole), status_of(wild)) == (200, 403)
        assert proxy.dialer.asked == [("10.0.0.5", 80)]

    async def test_a_name_that_answers_with_a_good_address_and_a_bad_one_gets_neither(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC, "169.254.169.254"])) as proxy:
            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert status_of(response) == 403
        assert proxy.dialer.asked == []

    async def test_a_domain_that_answers_from_inside_the_house_is_refused(self) -> None:
        async with running(names=Names(evil_example_org=["192.168.1.1"])) as proxy:
            response = await proxy.send(
                proxy.get("http://evil.example.org/", grant_header("*.example.org"))
            )

        assert status_of(response) == 403

    async def test_a_name_that_is_not_found_is_a_bad_gateway(self) -> None:
        async with running() as proxy:
            response = await proxy.send(
                proxy.get("http://gone.example.org/", grant_header("gone.example.org"))
            )

        assert status_of(response) == 502

    async def test_a_host_that_does_not_answer_is_a_bad_gateway(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            proxy.dialer.refuse.add(PUBLIC)

            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert status_of(response) == 502

    async def test_a_host_that_is_slow_to_answer_is_a_bad_gateway(self) -> None:
        async with running(
            names=Names(api_example_org=[PUBLIC]), limits=Limits(connect_timeout_s=0.1)
        ) as proxy:
            proxy.dialer.delay_s = 5

            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert status_of(response) == 502

    async def test_the_refusals_and_the_reasons_are_in_the_log_without_what_was_asked_for(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with running(names=Names(api_example_org=["10.0.0.5"])) as proxy:
            with caplog.at_level(logging.INFO, logger="factotum.egress"):
                await proxy.send(
                    proxy.get(
                        "http://api.example.org/private/path?token=hunter2",
                        grant_header("*.example.org", run="run-9"),
                    )
                )
                await proxy.send(
                    proxy.get("http://elsewhere.org/", grant_header("*.example.org", run="run-9"))
                )

        text = caplog.text
        assert "egress refused" in text
        records = [r for r in caplog.records if r.name == "factotum.egress"]
        assert {getattr(r, "run", None) for r in records} == {"run-9"}
        assert {getattr(r, "target", None) for r in records} == {"api.example.org", "elsewhere.org"}
        assert "hunter2" not in text
        assert "private/path" not in text

    async def test_what_is_allowed_is_in_the_log_too(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            with caplog.at_level(logging.INFO, logger="factotum.egress"):
                await proxy.send(
                    proxy.get("http://api.example.org/", grant_header("api.example.org"))
                )

        [record] = [r for r in caplog.records if r.name == "factotum.egress"]
        assert record.getMessage() == "egress allowed"
        assert (record.run, record.target, record.port, record.via) == (  # type: ignore[attr-defined]
            "run-1",
            "api.example.org",
            80,
            "http",
        )


class TestWhatIsNotPlainlyARequest:
    @pytest.mark.parametrize(
        ("raw", "status"),
        [
            (b"NONSENSE\r\n\r\n", 400),
            (b"GET http://a.org/ HTTP/2\r\n\r\n", 400),
            (b"GET http://a.org/ HTTP/1.1\r\n folded\r\n\r\n", 400),
            (b"GET http://a.org/ HTTP/1.1\r\nX: a\x00b\r\n\r\n", 400),
        ],
    )
    async def test_it_is_refused_before_anything_else_is_looked_at(
        self, raw: bytes, status: int
    ) -> None:
        async with running() as proxy:
            response = await proxy.send(raw)

        assert status_of(response) == status
        assert proxy.dialer.asked == []

    @pytest.mark.parametrize(
        ("extra", "status"),
        [
            ("Transfer-Encoding: chunked\r\n", 501),
            ("Upgrade: websocket\r\n", 501),
            ("Content-Length: 5\r\nContent-Length: 6\r\n", 400),
            ("Content-Length: 99999999999\r\n", 413),
        ],
    )
    async def test_a_body_that_has_no_plain_length_is_refused(
        self, extra: str, status: int
    ) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            raw = proxy.get("http://api.example.org/", grant_header("api.example.org"), extra=extra)

            response = await proxy.send(raw)

        assert status_of(response) == status
        assert proxy.dialer.asked == []

    async def test_a_page_that_is_not_in_the_clear_is_a_tunnel_and_not_this(self) -> None:
        async with running(names=Names(api_example_org=[PUBLIC])) as proxy:
            response = await proxy.send(
                proxy.get("https://api.example.org/", grant_header("api.example.org"))
            )

        assert status_of(response) == 400

    async def test_a_header_that_is_too_big_is_refused(self) -> None:
        async with running(limits=Limits(max_head_bytes=2048)) as proxy:
            response = await proxy.send(
                b"GET http://a.org/ HTTP/1.1\r\nX: " + b"a" * 5000 + b"\r\n\r\n"
            )

        assert status_of(response) == 431

    async def test_a_request_that_never_finishes_is_given_up_on(self) -> None:
        async with running(limits=Limits(header_timeout_s=0.2)) as proxy:
            response = await proxy.send(b"GET http://a.org/ HTTP/1.1\r\nHost: a.org\r\n")

        assert status_of(response) == 408

    async def test_a_body_that_does_not_come_is_given_up_on(self) -> None:
        async with running(
            names=Names(api_example_org=[PUBLIC]), limits=Limits(idle_timeout_s=0.2)
        ) as proxy:
            raw = (
                b"POST http://api.example.org/ HTTP/1.1\r\n"
                + f"Proxy-Authorization: {grant_header('api.example.org')}\r\n".encode()
                + b"Content-Length: 100\r\n\r\nonly a little"
            )

            response = await proxy.send(raw)

        assert response == b""
        assert proxy.upstream.received == []


class TestWhatGoesOnForTooLong:
    async def test_an_answer_bigger_than_a_connection_may_carry_is_cut(self) -> None:
        async with running(
            names=Names(api_example_org=[PUBLIC]), limits=Limits(max_bytes=100_000)
        ) as proxy:
            proxy.upstream.flood = 1_000_000

            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert 0 < len(response) <= 100_000

    async def test_a_tunnel_that_carries_too_much_is_cut(self) -> None:
        async with running(
            names=Names(api_example_org=[PUBLIC]), limits=Limits(max_bytes=10_000)
        ) as proxy:
            proxy.upstream.echo = True
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            auth = grant_header("api.example.org")
            writer.write(connect("api.example.org:443", auth))
            await reader.readuntil(b"\r\n\r\n")

            async def carry_on_and_on() -> None:
                for _ in range(200):
                    writer.write(b"x" * 1000)
                    await writer.drain()
                    await asyncio.wait_for(reader.readexactly(1000), 2)

            with pytest.raises((ConnectionError, asyncio.IncompleteReadError, TimeoutError)):
                await carry_on_and_on()
            writer.close()

    async def test_a_connection_that_says_nothing_is_cut(self) -> None:
        async with running(
            names=Names(api_example_org=[PUBLIC]), limits=Limits(idle_timeout_s=0.2)
        ) as proxy:
            proxy.upstream.silent = True

            response = await proxy.send(
                proxy.get("http://api.example.org/", grant_header("api.example.org"))
            )

        assert response == b""

    async def test_a_connection_that_goes_on_for_too_long_is_cut(self) -> None:
        async with running(
            names=Names(api_example_org=[PUBLIC]),
            limits=Limits(max_duration_s=0.3, idle_timeout_s=10),
        ) as proxy:
            proxy.upstream.echo = True
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            auth = grant_header("api.example.org")
            writer.write(connect("api.example.org:443", auth))
            await reader.readuntil(b"\r\n\r\n")

            rest = await asyncio.wait_for(reader.read(), 3)
            writer.close()

        assert rest == b""

    async def test_when_it_is_busy_the_next_one_is_told_to_try_again(self) -> None:
        async with running(
            names=Names(api_example_org=[PUBLIC]), limits=Limits(max_connections=1)
        ) as proxy:
            proxy.upstream.echo = True
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            auth = grant_header("api.example.org")
            writer.write(connect("api.example.org:443", auth))
            await reader.readuntil(b"\r\n\r\n")

            second = await proxy.send(proxy.get("http://api.example.org/", auth))
            writer.close()

        assert status_of(second) == 503
        assert b"Retry-After: 1" in second


def test_a_proxy_is_not_made_with_a_key_too_short_to_be_one() -> None:
    with pytest.raises(ValueError, match="at least"):
        EgressProxy(b"short")
