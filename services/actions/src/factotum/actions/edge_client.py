"""Having a device do something, through the edge that holds its connection.

The actions service decides what a device is to do, after the checks; the edge is what talks to it.
The commands are the vocabulary of ``factotum.contracts.commands`` and nothing else: a device is
never sent code, and a command it did not declare it can run has already been left out.

What became of a command is a ``CommandResult`` in every case: a device that is not connected, one
that took too long, an edge that cannot be reached. Whoever asked reads it and says so; nothing is
raised for a device that could not do what it was asked.
"""

import logging

import httpx
from pydantic import ValidationError

from factotum.common.devauth import PrincipalDelegation
from factotum.contracts.commands import CommandResult, CommandStatus, DeviceCommand
from factotum.contracts.edge import CommandRequest
from factotum.contracts.identity import Principal
from factotum.contracts.ids import DeviceId, TenantId

logger = logging.getLogger("factotum.actions.edge")


class EdgeDispatcher:
    """Gives commands to the devices that are connected to the edge."""

    def __init__(self, client: httpx.AsyncClient, delegation: PrincipalDelegation) -> None:
        self._client = client
        self._delegation = delegation

    async def dispatch(
        self, tenant_id: TenantId, device_id: DeviceId, command: DeviceCommand
    ) -> CommandResult:
        body = CommandRequest(command=command).model_dump(mode="json")
        headers = dict(self._delegation.headers(Principal.of_tenant(tenant_id)))
        try:
            response = await self._client.post(
                f"/v1/devices/{device_id}/commands", json=body, headers=headers
            )
        except httpx.HTTPError:
            logger.warning("the edge could not be reached", extra={"command": command.name})
            return CommandResult(
                status=CommandStatus.FAILED, detail="the edge could not be reached"
            )
        if response.status_code != 200:
            return CommandResult(
                status=CommandStatus.FAILED,
                detail=f"the edge answered with an error ({response.status_code})",
            )
        try:
            return CommandResult.model_validate_json(response.content)
        except ValidationError:
            return CommandResult(
                status=CommandStatus.FAILED,
                detail="the edge answered with something that is not the result of a command",
            )
