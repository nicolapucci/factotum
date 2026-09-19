"""A test double for system devices."""

from collections.abc import Iterable

from factotum.contracts.capabilities import Capability
from factotum.contracts.commands import CommandResult, CommandStatus, DeviceCommand
from factotum.contracts.ids import DeviceId
from factotum.contracts.testing import make_device_id
from factotum.devices.system_device import SystemDevice


class FakeSystemDevice(SystemDevice):
    """Records every command it receives instead of delivering it.

    Set ``result`` to make it answer something other than success, or ``offline`` to make it
    behave like a device that is not connected.
    """

    def __init__(
        self,
        capabilities: Iterable[Capability] = (Capability.AUDIO_IN, Capability.AUDIO_OUT),
        *,
        device_id: DeviceId | None = None,
        result: CommandResult | None = None,
    ) -> None:
        super().__init__(device_id or make_device_id(), capabilities)
        self.sent: list[DeviceCommand] = []
        self.result = result or CommandResult.ok()
        self.offline = False

    async def send(self, command: DeviceCommand) -> CommandResult:
        if self.offline:
            return CommandResult(status=CommandStatus.OFFLINE, detail="the device is offline")
        self.sent.append(command)
        return self.result
