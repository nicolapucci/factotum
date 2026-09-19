"""Helpers for testing adapters and the gateway without a network."""

import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from factotum.contracts.chat import ChatMessage, ChatRequest, EventUnion, MessageRole
from factotum.contracts.errors import FactotumError
from factotum.contracts.models import Locality, ModelBinding
from factotum.model_gateway.locality import infer_locality
from factotum.model_gateway.providers.base import ResolvedBinding

Handler = Callable[[httpx.Request], httpx.Response]


def mock_client(handler: Handler) -> httpx.AsyncClient:
    """An httpx client whose "network" is ``handler``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def resolved(
    binding: ModelBinding, *, base_url: str | None = None, api_key: str | None = None
) -> ResolvedBinding:
    """A resolved binding, the way the gateway would build it."""
    address = base_url or binding.base_url or "https://provider.example.org"
    return ResolvedBinding(
        binding=binding,
        base_url=address.rstrip("/"),
        api_key=SecretStr(api_key) if api_key else None,
        locality=binding.locality or infer_locality(address),
    )


def json_response(
    body: Any, *, status: int = 200, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=body, headers=dict(headers or {}))


def sse_body(*messages: tuple[str | None, Any]) -> bytes:
    """A server-sent events body. Each message is ``(event name or None, data)``; a data that is
    not a string is sent as JSON."""
    chunks: list[str] = []
    for event, data in messages:
        lines = [f"event: {event}"] if event else []
        payload = data if isinstance(data, str) else json.dumps(data)
        lines.append(f"data: {payload}")
        chunks.append("\n".join(lines))
    return ("\n\n".join(chunks) + "\n\n").encode()


def ndjson_body(*items: Any) -> bytes:
    """A newline-delimited JSON body."""
    return ("\n".join(json.dumps(item) for item in items) + "\n").encode()


def sse_response(*messages: tuple[str | None, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=sse_body(*messages), headers={"content-type": "text/event-stream"}
    )


def ndjson_response(*items: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=ndjson_body(*items), headers={"content-type": "application/x-ndjson"}
    )


class FakeServer:
    """A provider that answers as told, one response per request, and keeps what it received."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)

    @property
    def client(self) -> httpx.AsyncClient:
        return mock_client(self)

    @property
    def request(self) -> httpx.Request:
        """The last request received."""
        return self.requests[-1]

    @property
    def sent(self) -> dict[str, Any]:
        """The JSON body of the last request."""
        body: dict[str, Any] = json.loads(self.request.content)
        return body


def ask(text: str = "hello", **fields: Any) -> ChatRequest:
    """A chat request with one user message."""
    return ChatRequest(messages=(ChatMessage(role=MessageRole.USER, content=text),), **fields)


async def collect_events(
    stream: AsyncIterator[EventUnion],
) -> tuple[list[EventUnion], FactotumError | None]:
    """Everything a stream delivered, and the error that ended it, if one did."""
    got: list[EventUnion] = []
    try:
        async for event in stream:
            got.append(event)  # noqa: PERF401 - what came before a failure is what is being kept
    except FactotumError as error:
        return got, error
    return got, None


async def events(stream: AsyncIterator[EventUnion]) -> list[EventUnion]:
    """Everything a stream delivered; the error that ended it, if any, is raised."""
    got, error = await collect_events(stream)
    if error is not None:
        raise error
    return got


__all__ = [
    "FakeServer",
    "Handler",
    "Locality",
    "ask",
    "collect_events",
    "events",
    "json_response",
    "mock_client",
    "ndjson_body",
    "ndjson_response",
    "resolved",
    "sse_body",
    "sse_response",
]
