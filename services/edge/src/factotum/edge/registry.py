"""The devices the edge knows, and how it knows them.

A system device authenticates with a credential issued when it was paired (ADR 0005), and the
records of paired devices live in the hub (slice 7). Until then the registry is a file, for
development: it lists the devices that may connect, with what each is registered to do and whom it
acts for. It refuses to exist outside development and tests, like the other development
conveniences, and a credential in it is a placeholder: the hub keeps only a hash of what it issues.
"""

import hmac
from pathlib import Path
from typing import Protocol

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.errors import AuthError
from factotum.contracts.identity import OwnerKind, Principal, Role, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId
from factotum.devices.home import MIN_TOKEN_CHARS, device_id_for, parse_home

__all__ = [
    "MIN_TOKEN_CHARS",
    "DeviceRegistry",
    "FileDeviceRegistry",
    "device_id_for",
    "load_devices",
    "load_devices_file",
    "principal_of",
]


class DeviceRegistry(Protocol):
    async def authenticate(self, hardware_id: str, token: str) -> SystemDeviceRecord | None:
        """The device that has this hardware id and this credential, or nothing. Nothing says
        neither which of the two was wrong nor whether there is such a device."""
        ...

    async def get(self, tenant_id: TenantId, device_id: DeviceId) -> SystemDeviceRecord | None:
        """A device of the tenant, connected or not."""
        ...


def principal_of(device: SystemDeviceRecord, *, user_roles: frozenset[Role]) -> Principal:
    """Who a device acts for: a personal device acts as its owner, a shared one as the tenant."""
    if device.owner.kind is OwnerKind.USER:
        return Principal(
            tenant_id=device.tenant_id,
            device_id=device.id,
            acts_as=Subject.user(UserId(device.owner.id)),
            roles=user_roles,
        )
    return Principal(
        tenant_id=device.tenant_id, device_id=device.id, acts_as=Subject.tenant(device.tenant_id)
    )


class FileDeviceRegistry:
    """The devices of a YAML document: development only."""

    def __init__(
        self, settings: ServiceSettings, devices: list[tuple[SystemDeviceRecord, str]]
    ) -> None:
        if settings.environment not in {Environment.DEVELOPMENT, Environment.TEST}:
            raise AuthError(
                "the file of devices is disabled outside the development and test "
                f"environments (environment is {settings.environment.value!r}): devices are "
                "registered in the hub"
            )
        self._by_hardware_id: dict[str, list[tuple[SystemDeviceRecord, str]]] = {}
        self._by_id: dict[tuple[TenantId, DeviceId], SystemDeviceRecord] = {}
        for record, token in devices:
            self._by_id[(record.tenant_id, record.id)] = record
            self._by_hardware_id.setdefault(record.hardware_id, []).append((record, token))

    async def authenticate(self, hardware_id: str, token: str) -> SystemDeviceRecord | None:
        wanted = token.encode()
        found: SystemDeviceRecord | None = None
        # The same work whether there is such a device or not, and whichever of the two is wrong: a
        # device cannot be found out by how long it takes to be refused.
        candidates: list[tuple[SystemDeviceRecord | None, str]] = [
            *self._by_hardware_id.get(hardware_id, []),
            (None, "-" * len(token)),
        ]
        for record, issued in candidates:
            if hmac.compare_digest(wanted, issued.encode()) and record is not None:
                found = record
        return found

    async def get(self, tenant_id: TenantId, device_id: DeviceId) -> SystemDeviceRecord | None:
        return self._by_id.get((tenant_id, device_id))


def load_devices(
    text: str,
    settings: ServiceSettings,
    *,
    tenant_id: TenantId,
    user_id: UserId,
    source: str = "devices",
) -> FileDeviceRegistry:
    """The registry described by a YAML document: the system devices of the file of a home (see
    ``factotum.devices.home``). Its external devices and bindings are for the actions service, and
    the edge has nothing to do with them."""
    home = parse_home(text, tenant_id=tenant_id, user_id=user_id, source=source)
    return FileDeviceRegistry(settings, list(home.system))


def load_devices_file(
    path: Path, settings: ServiceSettings, *, tenant_id: TenantId, user_id: UserId
) -> FileDeviceRegistry:
    return load_devices(
        path.read_text(encoding="utf-8"),
        settings,
        tenant_id=tenant_id,
        user_id=user_id,
        source=path.name,
    )
