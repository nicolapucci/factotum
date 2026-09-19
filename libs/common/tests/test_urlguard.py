from collections.abc import Sequence

import httpx
import pytest

from factotum.common.urlguard import (
    GuardedTransport,
    IpClass,
    Resolver,
    UrlPolicy,
    check_url,
    classify_ip,
    guarded_client,
    vet_url,
)
from factotum.contracts.errors import UrlNotAllowedError

PUBLIC = "8.8.8.8"
REMOTE = UrlPolicy.remote()
LOCAL = UrlPolicy.local()


def resolver_for(table: dict[str, list[str]]) -> Resolver:
    async def resolve(host: str) -> Sequence[str]:
        if host not in table:
            raise OSError(f"unknown host {host}")
        return table[host]

    return resolve


async def never(host: str) -> Sequence[str]:
    raise AssertionError(f"an IP literal must not be resolved (asked for {host})")


class TestClassifyIp:
    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("8.8.8.8", IpClass.PUBLIC),
            ("2606:4700:4700::1111", IpClass.PUBLIC),
            ("127.0.0.1", IpClass.PRIVATE),
            ("::1", IpClass.PRIVATE),
            ("10.1.2.3", IpClass.PRIVATE),
            ("172.16.0.9", IpClass.PRIVATE),
            ("192.168.65.254", IpClass.PRIVATE),
            ("100.64.0.1", IpClass.PRIVATE),
            ("fd12:3456::1", IpClass.PRIVATE),
            ("169.254.169.254", IpClass.FORBIDDEN),
            ("169.254.170.2", IpClass.FORBIDDEN),
            ("fe80::1", IpClass.FORBIDDEN),
            ("fd00:ec2::254", IpClass.FORBIDDEN),
            ("100.100.100.200", IpClass.FORBIDDEN),
            ("0.0.0.0", IpClass.FORBIDDEN),  # noqa: S104 - classified, never bound
            ("::", IpClass.FORBIDDEN),
            ("224.0.0.1", IpClass.FORBIDDEN),
            ("240.0.0.1", IpClass.FORBIDDEN),
            ("::ffff:169.254.169.254", IpClass.FORBIDDEN),
            ("::ffff:127.0.0.1", IpClass.PRIVATE),
            ("::ffff:8.8.8.8", IpClass.PUBLIC),
            ("2002:a9fe:a9fe::1", IpClass.FORBIDDEN),  # 6to4 wrapping 169.254.169.254
            ("64:ff9b::7f00:1", IpClass.PRIVATE),  # NAT64 wrapping 127.0.0.1
        ],
    )
    def test_addresses_are_classified(self, address: str, expected: IpClass) -> None:
        assert classify_ip(address) is expected

    def test_something_that_is_not_an_address_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="does not appear"):
            classify_ip("example.org")


class TestRemotePolicy:
    """An endpoint on the public internet."""

    async def test_https_to_a_public_address_is_allowed(self) -> None:
        resolver = resolver_for({"api.example.org": [PUBLIC]})

        addresses = await vet_url("https://api.example.org/v1", REMOTE, resolver=resolver)

        assert addresses == (PUBLIC,)

    async def test_plain_http_to_a_public_address_is_refused(self) -> None:
        resolver = resolver_for({"api.example.org": [PUBLIC]})

        with pytest.raises(UrlNotAllowedError, match="needs https"):
            await check_url("http://api.example.org/v1", REMOTE, resolver=resolver)

    @pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "::1", "fd00::1"])
    async def test_a_name_that_resolves_to_the_private_network_is_refused(
        self, address: str
    ) -> None:
        resolver = resolver_for({"internal.example.org": [address]})

        with pytest.raises(UrlNotAllowedError, match="private-network"):
            await check_url("https://internal.example.org", REMOTE, resolver=resolver)

    async def test_a_name_that_resolves_to_a_public_and_a_private_address_is_refused(self) -> None:
        resolver = resolver_for({"sneaky.example.org": [PUBLIC, "10.0.0.5"]})

        with pytest.raises(UrlNotAllowedError, match="private-network"):
            await check_url("https://sneaky.example.org", REMOTE, resolver=resolver)

    async def test_an_ip_literal_is_checked_without_resolving_it(self) -> None:
        assert await vet_url("https://8.8.8.8/x", REMOTE, resolver=never) == ("8.8.8.8",)
        with pytest.raises(UrlNotAllowedError, match="private-network"):
            await check_url("https://192.168.1.10/x", REMOTE, resolver=never)

    async def test_a_number_written_as_a_decimal_ip_is_caught_after_resolution(self) -> None:
        # 2130706433 is 127.0.0.1; the operating system resolves it, and the result is checked.
        resolver = resolver_for({"2130706433": ["127.0.0.1"]})

        with pytest.raises(UrlNotAllowedError, match="private-network"):
            await check_url("https://2130706433/", REMOTE, resolver=resolver)


class TestLocalPolicy:
    """An endpoint the user declared local, such as an Ollama on the LAN."""

    @pytest.mark.parametrize(
        ("url", "table"),
        [
            ("http://192.168.1.50:11434/api/chat", {}),
            ("http://localhost:11434", {"localhost": ["127.0.0.1", "::1"]}),
            ("http://host.docker.internal:11434", {"host.docker.internal": ["192.168.65.254"]}),
            ("http://[::1]:8080/", {}),
        ],
    )
    async def test_private_addresses_over_plain_http_are_allowed(
        self, url: str, table: dict[str, list[str]]
    ) -> None:
        await check_url(url, LOCAL, resolver=resolver_for(table))

    async def test_a_public_address_still_needs_https(self) -> None:
        resolver = resolver_for({"api.example.org": [PUBLIC]})

        with pytest.raises(UrlNotAllowedError, match="needs https"):
            await check_url("http://api.example.org", LOCAL, resolver=resolver)


class TestAlwaysRefused:
    @pytest.mark.parametrize("policy", [REMOTE, LOCAL], ids=["remote", "local"])
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.170.2/v2/credentials",
            "http://[::ffff:169.254.169.254]/",
            "http://[fd00:ec2::254]/",
            "http://100.100.100.200/latest/meta-data",
            "http://169.254.10.10/",
            "http://0.0.0.0:8000/",
            "http://224.0.0.1/",
        ],
    )
    async def test_metadata_link_local_and_unroutable_addresses(
        self, url: str, policy: UrlPolicy
    ) -> None:
        with pytest.raises(UrlNotAllowedError, match=r"link-local|metadata|reserved|multicast"):
            await check_url(url, policy, resolver=never)

    @pytest.mark.parametrize("policy", [REMOTE, LOCAL], ids=["remote", "local"])
    @pytest.mark.parametrize("host", ["metadata.google.internal", "metadata", "instance-data"])
    async def test_metadata_host_names_even_when_they_resolve_to_a_public_address(
        self, host: str, policy: UrlPolicy
    ) -> None:
        resolver = resolver_for({host: [PUBLIC]})

        with pytest.raises(UrlNotAllowedError, match="metadata host"):
            await check_url(f"https://{host}/", policy, resolver=resolver)


class TestMalformedUrls:
    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            ("ftp://files.example.org/x", "scheme 'ftp'"),
            ("file:///etc/passwd", "scheme 'file'"),
            ("gopher://x.example.org", "scheme 'gopher'"),
            ("example.org/no-scheme", "scheme"),
            ("https:///no-host", "no host"),
            ("https://user:pw@api.example.org/", "credentials"),
            ("https://api.example.org:99999/", "malformed"),
        ],
    )
    async def test_urls_that_are_not_plain_http_endpoints_are_refused(
        self, url: str, reason: str
    ) -> None:
        with pytest.raises(UrlNotAllowedError, match=reason):
            await check_url(url, LOCAL, resolver=never)

    async def test_an_unresolvable_name_is_refused(self) -> None:
        with pytest.raises(UrlNotAllowedError, match="could not be resolved"):
            await check_url("https://nowhere.example.org", REMOTE, resolver=resolver_for({}))

    async def test_a_name_that_resolves_to_nothing_is_refused(self) -> None:
        resolver = resolver_for({"empty.example.org": []})

        with pytest.raises(UrlNotAllowedError, match="no address"):
            await check_url("https://empty.example.org", REMOTE, resolver=resolver)

    async def test_a_resolver_that_returns_garbage_is_refused(self) -> None:
        resolver = resolver_for({"odd.example.org": ["not-an-ip"]})

        with pytest.raises(UrlNotAllowedError, match="not an IP address"):
            await check_url("https://odd.example.org", REMOTE, resolver=resolver)

    async def test_secrets_in_a_refused_url_are_not_copied_into_the_error(self) -> None:
        with pytest.raises(UrlNotAllowedError) as caught:
            await check_url("ftp://x.example.org/?key=TOPSECRET123", REMOTE, resolver=never)

        assert "TOPSECRET123" not in str(caught.value.details)
        assert "TOPSECRET123" not in caught.value.message


class RecordingTransport(httpx.AsyncBaseTransport):
    """Stands in for the network and remembers what it was asked to send."""

    def __init__(self, redirect_to: str | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._redirect_to = redirect_to

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._redirect_to and len(self.requests) == 1:
            return httpx.Response(302, headers={"Location": self._redirect_to}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)


class TestGuardedTransport:
    async def test_it_connects_to_the_vetted_address_and_keeps_the_host_and_tls_name(self) -> None:
        inner = RecordingTransport()
        resolver = resolver_for({"api.example.org": [PUBLIC]})

        async with guarded_client(REMOTE, resolver=resolver, inner=inner) as client:
            response = await client.get("https://api.example.org:8443/v1/models?x=1")

        assert response.status_code == 200
        (sent,) = inner.requests
        assert sent.url.host == PUBLIC
        assert sent.url.port == 8443
        assert sent.url.path == "/v1/models"
        assert sent.headers["host"] == "api.example.org:8443"
        assert sent.extensions["sni_hostname"] == "api.example.org"

    async def test_an_ipv6_address_is_pinned_correctly(self) -> None:
        inner = RecordingTransport()
        resolver = resolver_for({"v6.example.org": ["2606:4700:4700::1111"]})

        async with guarded_client(REMOTE, resolver=resolver, inner=inner) as client:
            await client.get("https://v6.example.org/")

        assert inner.requests[0].url.host == "2606:4700:4700::1111"

    async def test_a_refused_request_never_reaches_the_network(self) -> None:
        inner = RecordingTransport()

        async with guarded_client(REMOTE, resolver=never, inner=inner) as client:
            with pytest.raises(UrlNotAllowedError):
                await client.get("http://169.254.169.254/latest/meta-data/")

        assert inner.requests == []

    async def test_the_request_body_and_method_are_preserved(self) -> None:
        inner = RecordingTransport()
        resolver = resolver_for({"api.example.org": [PUBLIC]})

        async with guarded_client(REMOTE, resolver=resolver, inner=inner) as client:
            await client.post("https://api.example.org/v1/chat", json={"model": "m"})

        (sent,) = inner.requests
        assert sent.method == "POST"
        assert sent.headers["content-type"] == "application/json"
        assert b'"model"' in await sent.aread()

    async def test_redirects_are_not_followed_by_default(self) -> None:
        inner = RecordingTransport(redirect_to="http://192.168.0.5/admin")
        resolver = resolver_for({"api.example.org": [PUBLIC]})

        async with guarded_client(REMOTE, resolver=resolver, inner=inner) as client:
            response = await client.get("https://api.example.org/")

        assert response.status_code == 302
        assert len(inner.requests) == 1

    async def test_every_hop_of_a_followed_redirect_is_vetted(self) -> None:
        inner = RecordingTransport(redirect_to="https://internal.example.org/admin")
        resolver = resolver_for({"api.example.org": [PUBLIC], "internal.example.org": ["10.0.0.5"]})

        async with guarded_client(
            REMOTE, resolver=resolver, inner=inner, follow_redirects=True
        ) as client:
            with pytest.raises(UrlNotAllowedError, match="private-network"):
                await client.get("https://api.example.org/")

        assert len(inner.requests) == 1  # the redirect was never sent

    async def test_a_followed_redirect_to_an_allowed_address_works(self) -> None:
        inner = RecordingTransport(redirect_to="https://other.example.org/v2")
        resolver = resolver_for({"api.example.org": [PUBLIC], "other.example.org": ["1.1.1.1"]})

        async with guarded_client(
            REMOTE, resolver=resolver, inner=inner, follow_redirects=True
        ) as client:
            response = await client.get("https://api.example.org/")

        assert response.status_code == 200
        assert [request.url.host for request in inner.requests] == [PUBLIC, "1.1.1.1"]

    async def test_a_local_policy_lets_a_lan_server_be_reached(self) -> None:
        inner = RecordingTransport()

        async with guarded_client(LOCAL, resolver=never, inner=inner) as client:
            response = await client.post("http://192.168.1.50:11434/api/chat", json={})

        assert response.status_code == 200
        assert inner.requests[0].url.host == "192.168.1.50"

    async def test_closing_the_transport_closes_the_inner_one(self) -> None:
        closed: list[bool] = []

        class Closing(RecordingTransport):
            async def aclose(self) -> None:
                closed.append(True)

        transport = GuardedTransport(REMOTE, inner=Closing())
        await transport.aclose()

        assert closed == [True]
