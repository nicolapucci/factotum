"""Answering an utterance, through the conversation service.

The edge never answers anything itself: it asks the conversation service for a turn, acting for
whom the device acts for (a person, or the tenant for a shared device), and takes what the service
streams: the events of a turn, as server-sent frames. What an answer says, which model wrote it and
where its passages came from are that service's business; what the edge does with them is decide
what a device can be sent (``parts``).
"""

import json
from collections.abc import AsyncGenerator
from typing import Any, Protocol

import httpx
from pydantic import TypeAdapter, ValidationError

from factotum.common.devauth import PrincipalDelegation
from factotum.contracts.conversation import (
    Failed,
    Finished,
    TurnEvent,
    TurnEventUnion,
    TurnRequest,
)
from factotum.contracts.errors import (
    ConversationUnavailableError,
    FactotumError,
    error_class_for,
)
from factotum.contracts.identity import Principal

_EVENTS: TypeAdapter[Any] = TypeAdapter(TurnEventUnion)


class TurnSource(Protocol):
    def turn(self, principal: Principal, message: str) -> AsyncGenerator[TurnEvent]:
        """The events of the answer to ``message``, acting for ``principal``. Raises a
        ``FactotumError`` when it cannot be answered at all (before the first event) or when the
        answer is cut short."""
        ...

    async def ready(self) -> bool:
        """Whether the service that answers is there to be asked."""
        ...


class ConversationClient:
    def __init__(self, client: httpx.AsyncClient, delegation: PrincipalDelegation) -> None:
        self._client = client
        self._delegation = delegation

    async def turn(self, principal: Principal, message: str) -> AsyncGenerator[TurnEvent]:
        body = TurnRequest(message=message).model_dump(mode="json", exclude_none=True)
        ended = False
        try:
            async with self._client.stream(
                "POST",
                "/v1/turns",
                json=body,
                headers=dict(self._delegation.headers(principal)),
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise _error_of(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # a blank line between frames, or a comment
                    event = _event_of(line.removeprefix("data:").strip())
                    ended = ended or isinstance(event, Finished | Failed)
                    yield event
        except httpx.HTTPError:
            raise ConversationUnavailableError(
                "the conversation service could not be reached"
            ) from None
        if not ended:
            raise ConversationUnavailableError("the answer stopped before it was finished")

    async def ready(self) -> bool:
        try:
            response = await self._client.get("/readyz", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200


def _event_of(payload: str) -> TurnEvent:
    try:
        event: TurnEvent = _EVENTS.validate_json(payload)
    except ValidationError:
        raise ConversationUnavailableError(
            "the conversation service answered with something that is not an event of a turn"
        ) from None
    return event


def _error_of(response: httpx.Response) -> FactotumError:
    """The typed error the conversation service answered with, for what it is: a caller that is
    told there is no chat model reacts to that, and not to a status."""
    try:
        error = json.loads(response.content)["error"]
        code, message = str(error["code"]), str(error["message"])
        details = error.get("details") or {}
    except (ValueError, KeyError, TypeError):
        return ConversationUnavailableError(
            f"the conversation service answered with an error ({response.status_code})"
        )
    kind = error_class_for(code)
    if kind is None:
        return ConversationUnavailableError(
            f"the conversation service answered with an error ({response.status_code})"
        )
    try:
        return kind(message, details=details)
    except TypeError:  # an error that needs more to be made than a message
        return ConversationUnavailableError(message)
