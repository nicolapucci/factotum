"""The devices the actions service knows about: which are there, whom they belong to, and which
skills drive which of them.

The records of devices live in the hub (slice 7). Until then this is a file, for development (the
one the edge reads too: ``factotum.devices.home``), and it refuses to exist outside development and
tests like the other development conveniences. What a caller may use is decided here and nowhere
else, by the same rule as everything else that is owned: nothing crosses a tenant, a tenant's
device is everybody's in it, a person's own is theirs alone.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from factotum.common.authorization import can_access
from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.devices import ExternalDeviceRecord, JsonScalar, SystemDeviceRecord
from factotum.contracts.errors import AuthError
from factotum.contracts.identity import Principal
from factotum.contracts.ids import DeviceId, TenantId, UserId
from factotum.devices.home import Home, parse_home


@dataclass(frozen=True, slots=True)
class BoundDevice:
    """An external device a skill is bound to, with the device's own parameters."""

    device: ExternalDeviceRecord
    params: dict[str, JsonScalar]


class DeviceDirectory(Protocol):
    async def system_device(
        self, tenant_id: TenantId, device_id: DeviceId
    ) -> SystemDeviceRecord | None:
        """A system device of the tenant, whoever is asking."""
        ...

    async def system_devices(self, principal: Principal) -> list[SystemDeviceRecord]:
        """The system devices the caller may use."""
        ...

    async def bound_devices(self, principal: Principal, skill_name: str) -> list[BoundDevice]:
        """The external devices the caller may use that this skill is bound to, by name."""
        ...


def named[Device: (SystemDeviceRecord, ExternalDeviceRecord)](
    devices: Sequence[Device], text: str
) -> list[Device]:
    """The devices that are called ``text``, by name or by an alias, ignoring case and the space
    around it. More than one means that it is not clear which was meant."""
    wanted = " ".join(text.casefold().split())
    return [
        device
        for device in devices
        if wanted
        in {" ".join(device.name.casefold().split())}
        | {" ".join(alias.casefold().split()) for alias in device.aliases}
    ]


class FileDeviceDirectory:
    """The devices of the file of a home."""

    def __init__(self, settings: ServiceSettings, home: Home) -> None:
        if settings.environment not in {Environment.DEVELOPMENT, Environment.TEST}:
            raise AuthError(
                "the file of devices is disabled outside the development and test "
                f"environments (environment is {settings.environment.value!r}): devices are "
                "registered in the hub"
            )
        self._system = [record for record, _ in home.system]
        self._external = {record.id: record for record in home.external}
        self._bindings = [b for b in home.bindings if b.enabled]

    async def system_device(
        self, tenant_id: TenantId, device_id: DeviceId
    ) -> SystemDeviceRecord | None:
        return next(
            (d for d in self._system if d.id == device_id and d.tenant_id == tenant_id), None
        )

    async def system_devices(self, principal: Principal) -> list[SystemDeviceRecord]:
        return [
            device
            for device in self._system
            if can_access(principal, resource_tenant_id=device.tenant_id, owner=device.owner)
        ]

    async def bound_devices(self, principal: Principal, skill_name: str) -> list[BoundDevice]:
        found: list[BoundDevice] = []
        for binding in self._bindings:
            device = self._external.get(binding.device_id)
            if (
                binding.skill == skill_name
                and device is not None
                and can_access(principal, resource_tenant_id=device.tenant_id, owner=device.owner)
            ):
                found.append(BoundDevice(device, dict(binding.params)))
        return sorted(found, key=lambda bound: bound.device.name)


def load_directory(
    path: Path, settings: ServiceSettings, *, tenant_id: TenantId, user_id: UserId
) -> FileDeviceDirectory:
    """The directory of the file at ``path``. Raises ``ValueError`` (saying where) when the file is
    not one, and ``FileNotFoundError`` when it is not there."""
    return FileDeviceDirectory(
        settings,
        parse_home(
            path.read_text(encoding="utf-8"), tenant_id=tenant_id, user_id=user_id, source=path.name
        ),
    )
