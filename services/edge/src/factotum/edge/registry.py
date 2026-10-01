"""The devices the edge knows, and how it knows them.

A system device authenticates with a credential issued when it was paired (ADR 0005). Once the hub
registers and pairs devices (slice 7b), ``HubDeviceRegistry`` fetches its tenant's system devices
once, in the lifespan, and checks a presented credential against the hash the hub keeps, without
asking the hub again for every connection. Until a token secret is configured the registry is a
file, for development: it lists the devices that may connect, with what each is registered to do
and whom it acts for, and a credential in it is a placeholder (a real one is never in the clear).
It refuses to exist outside development and tests, like the other development conveniences.
"""

import hmac
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import httpx

from factotum.common.config import Environment, ServiceSettings
from factotum.common.credentials import hash_credential
from factotum.common.devauth import PrincipalDelegation
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.errors import AuthError
from factotum.contracts.hub import DeviceList
from factotum.contracts.identity import OwnerKind, Principal, Role, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId
from factotum.devices.home import MIN_TOKEN_CHARS, device_id_for, parse_home

__all__ = [
    "MIN_TOKEN_CHARS",
    "DeviceRegistry",
    "FileDeviceRegistry",
    "HubDeviceRegistry",
    "device_id_for",
    "load_devices",
    "load_devices_file",
    "principal_of",
]

_DUMMY_HASH = hash_credential("not a device's credential, only here to cost the same time")


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


class HubDeviceRegistry:
    """The system devices of the hub's tenant: empty until ``reload`` is awaited once, in the
    lifespan, before the edge starts serving; a connection is then checked against what was fetched,
    never against the hub itself."""

    def __init__(
        self, client: httpx.AsyncClient, delegation: PrincipalDelegation, tenant_id: TenantId
    ) -> None:
        self._client = client
        self._delegation = delegation
        self._tenant_id = tenant_id
        self._by_hardware_id: dict[str, list[tuple[SystemDeviceRecord, str]]] = {}
        self._by_id: dict[tuple[TenantId, DeviceId], SystemDeviceRecord] = {}

    async def reload(self) -> None:
        """The device list of the tenant, asked for as the tenant itself: a service acts for the
        household, not for a person, to ask the hub for this."""
        headers: Mapping[str, str] = self._delegation.headers(Principal.of_tenant(self._tenant_id))
        response = await self._client.get("/v1/devices", headers=headers)
        response.raise_for_status()
        devices = DeviceList.model_validate_json(response.content)
        by_hardware_id: dict[str, list[tuple[SystemDeviceRecord, str]]] = {}
        by_id: dict[tuple[TenantId, DeviceId], SystemDeviceRecord] = {}
        for entry in devices.system:
            record = entry.device
            by_id[(record.tenant_id, record.id)] = record
            by_hardware_id.setdefault(record.hardware_id, []).append(
                (record, entry.credential_hash)
            )
        self._by_hardware_id, self._by_id = by_hardware_id, by_id

    async def authenticate(self, hardware_id: str, token: str) -> SystemDeviceRecord | None:
        computed = hash_credential(token)
        found: SystemDeviceRecord | None = None
        # The same work whether there is such a device or not, and whichever of the two is wrong: a
        # device cannot be found out by how long it takes to be refused.
        candidates: list[tuple[SystemDeviceRecord | None, str]] = [
            *self._by_hardware_id.get(hardware_id, []),
            (None, _DUMMY_HASH),
        ]
        for record, stored_hash in candidates:
            if hmac.compare_digest(computed, stored_hash) and record is not None:
                found = record
        return found

    async def get(self, tenant_id: TenantId, device_id: DeviceId) -> SystemDeviceRecord | None:
        return self._by_id.get((tenant_id, device_id))
