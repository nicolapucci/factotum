"""The devices that are connected, and how the rest of the platform reaches one.

A command for a device (from the actions service, later) has to find the connection the device
holds to *this* edge, wait for the device to say what became of it, and say so. Which edge holds
a device's connection is a question that arises only when there are several of them (a
``DeviceRouter`` over a bus, in a multi-tenant cloud: ADR 0009); here there is one, and this is
its table.
"""

from factotum.contracts.commands import CommandResult, CommandStatus, DeviceCommand
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.edge import CloseCode, ConnectedDevice
from factotum.contracts.ids import DeviceId, TenantId
from factotum.devices.system_device import SystemDevice
from factotum.edge.session import Session


class Sessions:
    """The sessions that are open, by whose they are. A device has at most one: when it connects
    again the old connection is closed, since it may be a dead one that has not been noticed yet."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[TenantId, DeviceId], Session] = {}

    async def attach(self, session: Session) -> None:
        previous = self._by_key.get(session.key)
        self._by_key[session.key] = session
        if previous is not None:
            await previous.close(CloseCode.REPLACED, "the device connected again")

    def detach(self, session: Session) -> None:
        # Only its own entry: the session that replaced it is not its to remove.
        if self._by_key.get(session.key) is session:
            del self._by_key[session.key]

    def get(self, tenant_id: TenantId, device_id: DeviceId) -> Session | None:
        return self._by_key.get((tenant_id, device_id))

    def connected(self, tenant_id: TenantId) -> list[ConnectedDevice]:
        """The tenant's devices that are connected, by name."""
        found: list[ConnectedDevice] = []
        for (tenant, _), session in self._by_key.items():
            if tenant != tenant_id or session.record is None:
                continue
            found.append(
                ConnectedDevice(
                    id=session.record.id,
                    name=session.record.name,
                    hardware_id=session.record.hardware_id,
                    capabilities=session.capabilities,
                    display=session.display,
                    session_id=session.session_id,
                    connected_at=session.connected_at,
                )
            )
        return sorted(found, key=lambda device: device.name)

    async def close_all(self, code: CloseCode = CloseCode.GOING_AWAY) -> None:
        for session in list(self._by_key.values()):
            await session.close(code, "the edge is stopping")

    def __len__(self) -> int:
        return len(self._by_key)


class EdgeDevice(SystemDevice):
    """A system device as the rest of the platform holds it: its methods (``speak``,
    ``set_volume``...) reach the device over its connection and wait for its answer."""

    def __init__(
        self, sessions: Sessions, record: SystemDeviceRecord, *, timeout_s: float | None = None
    ) -> None:
        super().__init__(record.id, record.capabilities)
        self._sessions = sessions
        self._tenant_id = record.tenant_id
        self._timeout_s = timeout_s

    async def send(self, command: DeviceCommand) -> CommandResult:
        session = self._sessions.get(self._tenant_id, self.device_id)
        if session is None:
            return CommandResult(status=CommandStatus.OFFLINE, detail="the device is not connected")
        return await session.command(command, self._timeout_s)
