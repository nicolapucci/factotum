"""Asking a model to answer, through the model gateway: whole, streamed, and what a role can do.

A service asks for a *role* (``chat.main``, ``chat.fast``) on behalf of an owner and never learns
which model or provider answered. A failure comes back as the typed error the gateway raised.
"""

from collections.abc import AsyncGenerator
from typing import Protocol

import httpx
from pydantic import TypeAdapter, ValidationError

from factotum.contracts.chat import ChatEvent, ChatRequest, ChatResult, EventUnion
from factotum.contracts.errors import GatewayError, GatewayUnavailableError
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole, Profile
from factotum.model_client.gateway import Delegation, error_from

_EVENTS: TypeAdapter[EventUnion] = TypeAdapter(ChatEvent)
_DATA = "data: "


class Chat(Protocol):
    async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
        """The whole answer. With a ``response_schema``, a validated JSON value is in ``parsed``."""
        ...

    def chat_stream(self, owner: Owner, request: ChatRequest) -> AsyncGenerator[EventUnion]:
        """The answer as it is produced. A failure before the first event is raised, typed, when the
        stream is first read; one after it arrives as an ``ErrorEvent``."""
        ...

    async def profile(self, owner: Owner, role: ModelRole) -> Profile:
        """What ``role`` can do for ``owner``: the size of what it takes in and gives out."""
        ...


class GatewayChat:
    def __init__(self, client: httpx.AsyncClient, delegation: Delegation) -> None:
        self._client = client
        self._delegation = delegation

    async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
        body = request.model_copy(update={"stream": False}).model_dump(mode="json")
        try:
            response = await self._client.post(
                "/v1/chat", json=body, headers=dict(self._delegation.headers(owner))
            )
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway could not be reached") from None
        if response.status_code != 200:
            raise error_from(response, request.role)
        try:
            return ChatResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayError(
                "the model gateway answered with something that is not a chat answer"
            ) from None

    async def chat_stream(self, owner: Owner, request: ChatRequest) -> AsyncGenerator[EventUnion]:
        body = request.model_copy(update={"stream": True}).model_dump(mode="json")
        headers = dict(self._delegation.headers(owner))
        try:
            async with self._client.stream("POST", "/v1/chat", json=body, headers=headers) as sent:
                if sent.status_code != 200:
                    await sent.aread()
                    raise error_from(sent, request.role)
                async for line in sent.aiter_lines():
                    if line.startswith(_DATA):
                        yield self._event(line[len(_DATA) :])
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway stopped answering") from None

    @staticmethod
    def _event(payload: str) -> EventUnion:
        try:
            return _EVENTS.validate_json(payload)
        except ValidationError:
            raise GatewayError(
                "the model gateway sent something that is not a chat event"
            ) from None

    async def profile(self, owner: Owner, role: ModelRole) -> Profile:
        try:
            response = await self._client.get(
                f"/v1/profiles/{role.value}", headers=dict(self._delegation.headers(owner))
            )
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway could not be reached") from None
        if response.status_code != 200:
            raise error_from(response, role)
        try:
            return Profile.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayError(
                "the model gateway answered with something that is not a profile"
            ) from None
