"""What the proxy reads of a request, and what it sends on: nothing that is not plainly one."""

import base64

import pytest

from factotum.common import egress
from factotum.egress.proxy import (
    Limits,
    Refusal,
    Request,
    basic_credentials,
    body_length,
    forwarded_head,
    parse_absolute,
    parse_authority,
    parse_head,
)
from factotum.egress.testing import KEY, NOW


def head(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def request(*headers: str, method: str = "GET", target: str = "http://a.org/") -> Request:
    return parse_head(head(f"{method} {target} HTTP/1.1", *headers))


def refused(call: object) -> Refusal:
    with pytest.raises(Refusal) as raised:
        call()  # type: ignore[operator]
    return raised.value


class TestTheHead:
    def test_a_request_is_a_method_a_target_a_version_and_headers(self) -> None:
        made = parse_head(head("GET http://a.org/x?y=1 HTTP/1.1", "Host: a.org", "Accept: */*"))

        assert (made.method, made.target, made.version) == ("GET", "http://a.org/x?y=1", "HTTP/1.1")
        assert made.headers == (("Host", "a.org"), ("Accept", "*/*"))

    def test_a_header_is_found_by_its_name_in_any_case(self) -> None:
        made = request("Content-LENGTH: 3", "X-A: 1", "x-a: 2")

        assert made.header("content-length") == "3"
        assert made.all("X-A") == ["1", "2"]
        assert made.header("Missing") is None

    def test_the_space_around_a_value_is_not_part_of_it(self) -> None:
        assert request("X-A:   spaced \t ").header("X-A") == "spaced"

    @pytest.mark.parametrize("version", ["HTTP/1.0", "HTTP/1.1"])
    def test_the_versions_of_http_it_speaks(self, version: str) -> None:
        assert parse_head(head(f"GET / {version}")).version == version

    @pytest.mark.parametrize(
        "lines",
        [
            ["GET /"],
            ["GET / HTTP/1.1 extra"],
            ["get http://a.org/ HTTP/1.1"],
            ["GET  HTTP/1.1"],
            ["G3T http://a.org/ HTTP/1.1"],
            ["GET http://a.org/ HTTP/2"],
            ["GET http://a.org/ HTTP/0.9"],
            ["GET http://a.org/ HTTP/1.1", " folded: header"],
            ["GET http://a.org/ HTTP/1.1", "\tfolded: header"],
            ["GET http://a.org/ HTTP/1.1", "no colon here"],
            ["GET http://a.org/ HTTP/1.1", "Bad Name: value"],
            ["GET http://a.org/ HTTP/1.1", ": empty"],
            ["GET http://a.org/ HTTP/1.1", "X-A: a\x00b"],
            ["GET http://a.org/ HTTP/1.1", "X-A: a\x7fb"],
            ["GET http://a.org/ HTTP/1.1", "X-A: a\nX-B: injected"],
            ["GET http://a.org/ HTTP/1.1", "X-A: a\rb"],
        ],
    )
    def test_what_is_not_plainly_a_request_is_refused(self, lines: list[str]) -> None:
        assert refused(lambda: parse_head(head(*lines))).status == 400


class TestWhereARequestIsFor:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("example.org:443", ("example.org", 443)),
            ("API.Example.org.:8443", ("api.example.org", 8443)),
            ("192.168.1.20:443", ("192.168.1.20", 443)),
        ],
    )
    def test_a_tunnel_is_to_a_host_and_a_port(self, target: str, expected: tuple[str, int]) -> None:
        assert parse_authority(target) == expected

    @pytest.mark.parametrize("target", ["example.org", "example.org:", ":443", "example.org:0"])
    def test_a_tunnel_that_says_neither_is_refused(self, target: str) -> None:
        assert refused(lambda: parse_authority(target)).status == 400

    @pytest.mark.parametrize(
        "target", ["example.org:99999", "example.org:http", "a:b:c", "example.org:²"]
    )
    def test_a_port_that_is_not_one_is_refused(self, target: str) -> None:
        assert refused(lambda: parse_authority(target)).status in {400, 403}

    @pytest.mark.parametrize("target", ["[::1]:443", "user@example.org:443", "a/b:443"])
    def test_a_host_that_is_odd_is_not_connected_to(self, target: str) -> None:
        assert refused(lambda: parse_authority(target)).status == 403

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("http://example.org/", ("example.org", 80, "/")),
            ("http://example.org", ("example.org", 80, "/")),
            ("http://Example.org:8123/api/x?y=1&z=2", ("example.org", 8123, "/api/x?y=1&z=2")),
            ("http://192.168.1.20:8123/api", ("192.168.1.20", 8123, "/api")),
            ("http://example.org/a%20b#fragment", ("example.org", 80, "/a%20b")),
        ],
    )
    def test_a_request_in_the_clear_is_for_a_host_a_port_and_a_path(
        self, target: str, expected: tuple[str, int, str]
    ) -> None:
        assert parse_absolute(target) == expected

    @pytest.mark.parametrize(
        "target",
        [
            "/path",
            "https://example.org/",
            "ftp://example.org/",
            "http://",
            "http:///x",
            "http://a:99999/",
        ],
    )
    def test_what_is_not_an_address_in_the_clear_is_refused(self, target: str) -> None:
        assert refused(lambda: parse_absolute(target)).status == 400

    @pytest.mark.parametrize(
        "target",
        ["http://user@example.org/", "http://user:pass@example.org/", "http://@example.org/"],
    )
    def test_credentials_in_an_address_are_refused(self, target: str) -> None:
        assert refused(lambda: parse_absolute(target)).status == 403


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class TestTheGrantAPasswordCarries:
    def test_it_is_the_password_of_the_user_of_a_run(self) -> None:
        token = egress.issue(KEY, run="r", hosts=["a.org"], expires_at=NOW)

        assert (
            basic_credentials(request(f"Proxy-Authorization: {basic(egress.USER, token)}")) == token
        )

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "Bearer abc",
            "Basic",
            "Basic %%%",
            "Basic " + base64.b64encode(b"no-colon").decode(),
            "Basic " + base64.b64encode(b"someone:token").decode(),
            "Basic " + base64.b64encode("skill:é".encode()).decode(),
            "Basic " + base64.b64encode(b"\xff\xfe:x").decode(),
        ],
    )
    def test_anything_else_asks_for_a_grant(self, header: str | None) -> None:
        headers = [] if header is None else [f"Proxy-Authorization: {header}"]

        refusal = refused(lambda: basic_credentials(request(*headers)))

        assert refusal.status == 407
        assert ("Proxy-Authenticate", 'Basic realm="egress"') in refusal.headers

    def test_a_token_may_have_a_colon_in_it(self) -> None:
        assert basic_credentials(request(f"Proxy-Authorization: {basic('skill', 'a:b')}")) == "a:b"


class TestTheBodyOfARequest:
    def test_no_length_is_no_body(self) -> None:
        assert body_length(request(), Limits()) == 0

    def test_a_length_is_a_length(self) -> None:
        assert body_length(request("Content-Length: 12"), Limits()) == 12

    def test_the_same_length_said_twice_is_the_same_length(self) -> None:
        assert body_length(request("Content-Length: 5", "Content-Length: 5"), Limits()) == 5

    def test_two_lengths_are_a_request_that_could_be_read_two_ways(self) -> None:
        assert (
            refused(
                lambda: body_length(request("Content-Length: 5", "Content-Length: 6"), Limits())
            ).status
            == 400
        )

    @pytest.mark.parametrize("length", ["-1", "1.5", "abc", "", "0x10", "²", "Ù¡"])
    def test_a_length_that_is_not_digits_is_refused(self, length: str) -> None:
        assert refused(
            lambda: body_length(request(f"Content-Length: {length}"), Limits())
        ).status in {400, 413}

    def test_a_body_that_is_too_big_is_refused_before_it_is_read(self) -> None:
        limits = Limits(max_bytes=100)

        assert body_length(request("Content-Length: 100"), limits) == 100
        assert refused(lambda: body_length(request("Content-Length: 101"), limits)).status == 413

    @pytest.mark.parametrize("value", ["chunked", "gzip, chunked", "identity"])
    def test_a_body_that_has_no_length_is_not_sent_on(self, value: str) -> None:
        refusal = refused(lambda: body_length(request(f"Transfer-Encoding: {value}"), Limits()))

        assert refusal.status == 501

    def test_a_length_and_an_encoding_together_are_refused(self) -> None:
        made = request("Content-Length: 4", "Transfer-Encoding: chunked")

        assert refused(lambda: body_length(made, Limits())).status == 501

    def test_a_connection_that_is_to_become_another_is_refused(self) -> None:
        assert refused(lambda: body_length(request("Upgrade: websocket"), Limits())).status == 501


class TestWhatIsSentOn:
    def test_the_request_is_sent_as_it_was_asked_to_the_host_that_was_asked_for(self) -> None:
        made = request(
            "Host: lies.example",
            "Authorization: Bearer secret",
            "Content-Type: application/json",
            method="POST",
        )

        sent = forwarded_head(made, "homeassistant.local", 8123, "/api/x?y=1", 5).decode()

        assert sent.startswith("POST /api/x?y=1 HTTP/1.1\r\nHost: homeassistant.local:8123\r\n")
        assert "Authorization: Bearer secret\r\n" in sent
        assert "Content-Type: application/json\r\n" in sent
        assert "Content-Length: 5\r\n" in sent
        assert sent.endswith("Connection: close\r\n\r\n")
        assert "lies.example" not in sent

    def test_the_port_of_the_clear_is_not_written(self) -> None:
        assert "Host: a.org\r\n" in forwarded_head(request(), "a.org", 80, "/", 0).decode()

    @pytest.mark.parametrize(
        "name",
        [
            "Proxy-Authorization",
            "Proxy-Connection",
            "Connection",
            "Keep-Alive",
            "TE",
            "Trailer",
            "Upgrade",
            "Expect",
            "Transfer-Encoding",
        ],
    )
    def test_what_is_between_the_client_and_the_proxy_is_not_sent_on(self, name: str) -> None:
        sent = forwarded_head(request(f"{name}: x"), "a.org", 80, "/", 0).decode()

        assert f"{name}: x" not in sent
        assert sent.count("Connection: close") == 1

    def test_the_grant_never_reaches_the_host(self) -> None:
        token = egress.issue(KEY, run="r", hosts=["a.org"], expires_at=NOW)
        made = request(f"Proxy-Authorization: {basic(egress.USER, token)}")

        assert token not in forwarded_head(made, "a.org", 80, "/", 0).decode()

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
    def test_a_request_that_is_expected_to_have_a_body_says_how_long_it_is(
        self, method: str
    ) -> None:
        assert (
            "Content-Length: 0\r\n"
            in forwarded_head(request(method=method), "a.org", 80, "/", 0).decode()
        )

    def test_a_request_that_has_none_says_nothing_of_it(self) -> None:
        assert "Content-Length" not in forwarded_head(request(), "a.org", 80, "/", 0).decode()
