import email.utils
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from factotum.contracts.errors import (
    ContextTooLongError,
    InvalidRequestError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitedError,
)
from factotum.contracts.models import ProviderKind
from factotum.model_gateway.providers.base import (
    SseMessage,
    error_body,
    error_for_status,
    iter_ndjson,
    iter_sse,
    parse_json,
    parse_sse_json,
    post_json,
    retry_after_seconds,
    stream_post,
    transport_errors,
)
from factotum.model_gateway.testing import mock_client, ndjson_response, sse_response

OPENAI = ProviderKind.OPENAI


class TestErrorForStatus:
    @pytest.mark.parametrize(
        ("status", "expected", "retryable"),
        [
            (401, ProviderAuthError, False),
            (403, ProviderAuthError, False),
            (408, ProviderTimeoutError, True),
            (413, ContextTooLongError, False),
            (429, RateLimitedError, True),
            (400, InvalidRequestError, False),
            (404, InvalidRequestError, False),
            (409, InvalidRequestError, False),
            (422, InvalidRequestError, False),
            (500, ProviderUnavailableError, True),
            (502, ProviderUnavailableError, True),
            (503, ProviderUnavailableError, True),
            (529, ProviderUnavailableError, True),
            (418, ProviderError, False),
        ],
    )
    def test_each_status_becomes_a_typed_error(
        self, status: int, expected: type[ProviderError], retryable: bool
    ) -> None:
        error = error_for_status(status, "nope", provider=OPENAI)

        assert type(error) is expected
        assert error.retryable is retryable
        assert error.details == {"provider": "openai", "status": status}

    def test_the_provider_code_is_kept_in_the_details(self) -> None:
        error = error_for_status(400, "bad", provider=OPENAI, provider_code="invalid_model")

        assert error.details["provider_code"] == "invalid_model"

    def test_a_key_quoted_by_the_provider_is_redacted(self) -> None:
        message = "Incorrect API key provided: sk-proj-abcdefghijklmnopqrstuvwxyz123456"

        error = error_for_status(401, message, provider=OPENAI)

        assert "abcdefghijklmnop" not in str(error)
        assert "[REDACTED]" in str(error)

    def test_a_very_long_message_is_cut(self) -> None:
        error = error_for_status(500, "x" * 5000, provider=OPENAI)

        assert len(str(error)) < 600

    def test_an_empty_message_still_says_something(self) -> None:
        assert "HTTP 500" in str(error_for_status(500, "  ", provider=OPENAI))

    def test_a_rate_limit_carries_the_retry_after_header(self) -> None:
        error = error_for_status(429, "slow", provider=OPENAI, headers={"retry-after": "7"})

        assert isinstance(error, RateLimitedError)
        assert error.retry_after_s == 7.0


class TestRetryAfter:
    @pytest.mark.parametrize(("raw", "expected"), [("2", 2.0), ("1.5", 1.5), ("0", 0.0)])
    def test_seconds(self, raw: str, expected: float) -> None:
        assert retry_after_seconds({"retry-after": raw}) == expected

    def test_an_http_date(self) -> None:
        when = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)

        seconds = retry_after_seconds({"retry-after": when})

        assert seconds is not None
        assert 25 <= seconds <= 31

    def test_a_date_in_the_past_means_no_wait(self) -> None:
        when = email.utils.format_datetime(datetime.now(UTC) - timedelta(hours=1), usegmt=True)

        assert retry_after_seconds({"retry-after": when}) == 0.0

    @pytest.mark.parametrize("raw", ["soon", "", "12 parsecs"])
    def test_garbage_is_ignored(self, raw: str) -> None:
        assert retry_after_seconds({"retry-after": raw}) is None

    def test_no_header_means_no_hint(self) -> None:
        assert retry_after_seconds({}) is None

    def test_an_absurd_wait_is_capped(self) -> None:
        assert retry_after_seconds({"retry-after": "999999"}) == 3600.0

    def test_a_negative_wait_becomes_zero(self) -> None:
        assert retry_after_seconds({"retry-after": "-5"}) == 0.0


class TestParsing:
    def test_a_json_body_is_parsed(self) -> None:
        assert parse_json(httpx.Response(200, json={"a": 1}), OPENAI) == {"a": 1}

    def test_a_body_that_is_not_json_is_a_provider_failure(self) -> None:
        with pytest.raises(ProviderUnavailableError, match="not valid JSON"):
            parse_json(httpx.Response(200, content=b"<html>oops</html>"), OPENAI)

    def test_an_error_body_that_is_not_json_is_none(self) -> None:
        assert error_body(httpx.Response(502, content=b"Bad gateway")) is None
        assert error_body(httpx.Response(400, json={"error": "x"})) == {"error": "x"}


async def collect_sse(body: bytes) -> list[SseMessage]:
    response = httpx.Response(200, content=body)
    return [message async for message in iter_sse(response)]


class TestServerSentEvents:
    async def test_named_and_unnamed_events(self) -> None:
        messages = await collect_sse(b"event: a\ndata: 1\n\ndata: 2\n\n")

        assert messages == [SseMessage("a", "1"), SseMessage(None, "2")]

    async def test_multi_line_data_is_joined(self) -> None:
        assert await collect_sse(b"data: one\ndata: two\n\n") == [SseMessage(None, "one\ntwo")]

    async def test_comments_are_ignored(self) -> None:
        body = b": keep-alive\n\ndata: real\n\n: another\n"

        assert await collect_sse(body) == [SseMessage(None, "real")]

    async def test_a_last_event_without_the_closing_blank_line_is_still_delivered(self) -> None:
        assert await collect_sse(b"data: tail") == [SseMessage(None, "tail")]

    async def test_carriage_returns_are_handled(self) -> None:
        assert await collect_sse(b"event: x\r\ndata: 1\r\n\r\n") == [SseMessage("x", "1")]

    async def test_the_space_after_the_colon_is_optional(self) -> None:
        assert await collect_sse(b"data:tight\n\n") == [SseMessage(None, "tight")]

    async def test_a_done_marker_is_just_data(self) -> None:
        assert await collect_sse(b"data: [DONE]\n\n") == [SseMessage(None, "[DONE]")]

    def test_the_json_of_an_event_is_parsed(self) -> None:
        assert parse_sse_json(SseMessage(None, '{"a": 1}'), OPENAI) == {"a": 1}

    def test_an_event_that_is_not_json_is_a_provider_failure(self) -> None:
        with pytest.raises(ProviderUnavailableError, match="not JSON"):
            parse_sse_json(SseMessage(None, "hello"), OPENAI)


class TestNewlineDelimitedJson:
    async def test_each_line_is_a_value(self) -> None:
        response = ndjson_response({"a": 1}, {"b": 2})

        assert [item async for item in iter_ndjson(response, OPENAI)] == [{"a": 1}, {"b": 2}]

    async def test_blank_lines_are_skipped(self) -> None:
        response = httpx.Response(200, content=b'{"a": 1}\n\n  \n{"b": 2}')

        assert len([item async for item in iter_ndjson(response, OPENAI)]) == 2

    async def test_a_line_that_is_not_json_is_a_provider_failure(self) -> None:
        response = httpx.Response(200, content=b'{"a": 1}\nnope\n')

        with pytest.raises(ProviderUnavailableError, match="not JSON"):
            [item async for item in iter_ndjson(response, OPENAI)]


class TestTransportErrors:
    def test_a_timeout_becomes_a_provider_timeout(self) -> None:
        with pytest.raises(ProviderTimeoutError), transport_errors(OPENAI):
            raise httpx.ReadTimeout("read timed out")

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectError("refused"), httpx.ReadError("reset"), httpx.RemoteProtocolError("x")],
    )
    def test_other_transport_failures_mean_the_provider_is_unavailable(
        self, error: httpx.TransportError
    ) -> None:
        with pytest.raises(ProviderUnavailableError), transport_errors(OPENAI):
            raise error

    def test_the_message_of_the_transport_error_is_not_repeated(self) -> None:
        with pytest.raises(ProviderUnavailableError) as caught, transport_errors(OPENAI):
            raise httpx.ConnectError("could not reach https://x.example.org/v1?key=TOPSECRET123")

        assert "TOPSECRET123" not in str(caught.value)
        assert "ConnectError" in str(caught.value)

    def test_our_own_errors_pass_through(self) -> None:
        with pytest.raises(InvalidRequestError), transport_errors(OPENAI):
            raise InvalidRequestError("mine")


def map_error(response: httpx.Response) -> ProviderError:
    return error_for_status(response.status_code, "boom", provider=OPENAI)


class TestPosting:
    async def test_a_json_answer_is_returned(self) -> None:
        client = mock_client(lambda request: httpx.Response(200, json={"ok": True}))

        body = await post_json(
            client,
            "https://x.example.org/y",
            headers={},
            payload={},
            provider=OPENAI,
            map_error=map_error,
        )

        assert body == {"ok": True}

    async def test_headers_and_payload_are_sent(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        await post_json(
            mock_client(handler),
            "https://x.example.org/y",
            headers={"x-test": "1"},
            payload={"a": 1},
            provider=OPENAI,
            map_error=map_error,
        )

        assert seen[0].headers["x-test"] == "1"
        assert seen[0].content == b'{"a":1}'

    async def test_an_error_status_is_mapped(self) -> None:
        client = mock_client(lambda request: httpx.Response(503, json={}))

        with pytest.raises(ProviderUnavailableError):
            await post_json(
                client,
                "https://x.example.org/y",
                headers={},
                payload={},
                provider=OPENAI,
                map_error=map_error,
            )

    async def test_a_dead_server_is_mapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderUnavailableError):
            await post_json(
                mock_client(handler),
                "https://x.example.org/y",
                headers={},
                payload={},
                provider=OPENAI,
                map_error=map_error,
            )


class BreakingStream(httpx.AsyncByteStream):
    """Delivers a first chunk and then fails, like a connection dropped mid-answer."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: first\n\n"
        raise httpx.ReadError("connection reset")


class TestStreaming:
    async def test_the_response_is_streamed(self) -> None:
        client = mock_client(lambda request: sse_response((None, "hello")))

        async with stream_post(
            client,
            "https://x.example.org/y",
            headers={},
            payload={},
            provider=OPENAI,
            map_error=map_error,
        ) as response:
            messages = [message async for message in iter_sse(response)]

        assert messages == [SseMessage(None, "hello")]

    async def test_an_error_status_raises_before_anything_is_read(self) -> None:
        client = mock_client(lambda request: httpx.Response(429, json={}))

        with pytest.raises(RateLimitedError):
            async with stream_post(
                client,
                "https://x.example.org/y",
                headers={},
                payload={},
                provider=OPENAI,
                map_error=map_error,
            ):
                pytest.fail("the body must not be reached")

    async def test_a_connection_dropped_mid_stream_is_mapped(self) -> None:
        client = mock_client(lambda request: httpx.Response(200, stream=BreakingStream()))

        async def read_everything() -> None:
            async with stream_post(
                client,
                "https://x.example.org/y",
                headers={},
                payload={},
                provider=OPENAI,
                map_error=map_error,
            ) as response:
                async for _ in iter_sse(response):
                    pass

        with pytest.raises(ProviderUnavailableError, match="ReadError"):
            await read_everything()
