"""Asking whether a message is a request to do something, through the actions service.

This service never decides an action. Before it searches the documents it asks the actions service
whether the message asks for something to be done, acting for whom the turn acts for (a person, or
the tenant for a shared device) and on the device the message came from, because what a device may
have done is decided there and nowhere else. What comes back is either "no, that is a question" or
what became of the request, with the words to say in the language of the message.

The message alone goes: nothing from the documents or from the web is in it, so ``untrusted`` is
never true here (see ``authorize``).
"""

from typing import Protocol

import httpx
from pydantic import ValidationError

from factotum.common.devauth import PrincipalDelegation
from factotum.contracts.actions import ActOutcome, ActRequest
from factotum.contracts.errors import ActionsUnavailableError
from factotum.contracts.identity import Principal


class Actions(Protocol):
    async def act(self, principal: Principal, message: str) -> ActOutcome:
        """What ``message`` asks to be done for ``principal``, and what became of it. Raises
        ``ActionsUnavailableError`` when that cannot be told."""
        ...


class ActionsClient:
    def __init__(self, client: httpx.AsyncClient, delegation: PrincipalDelegation) -> None:
        self._client = client
        self._delegation = delegation

    async def act(self, principal: Principal, message: str) -> ActOutcome:
        body = ActRequest(message=message).model_dump(mode="json")
        try:
            response = await self._client.post(
                "/v1/act", json=body, headers=dict(self._delegation.headers(principal))
            )
        except httpx.HTTPError:
            raise ActionsUnavailableError("the actions service could not be reached") from None
        if response.status_code != 200:
            raise ActionsUnavailableError(
                f"the actions service answered with an error ({response.status_code})"
            )
        try:
            return ActOutcome.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ActionsUnavailableError(
                "the actions service answered with something that is not an outcome"
            ) from None
