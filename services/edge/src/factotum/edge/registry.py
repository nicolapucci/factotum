"""The devices the edge knows, and how it knows them.

A system device authenticates with a credential issued when it was paired (ADR 0005), and the
records of paired devices live in the hub (slice 7). Until then the registry is a file, for
development: it lists the devices that may connect, with what each is registered to do and whom it
acts for. It refuses to exist outside development and tests, like the other development
conveniences, and a credential in it is a placeholder: the hub keeps only a hash of what it issues.
"""

import hmac
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import ValidationError

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.devices import DevicePolicy, SystemDeviceRecord
from factotum.contracts.errors import AuthError
from factotum.contracts.identity import Owner, OwnerKind, Principal, Role, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId

MIN_TOKEN_CHARS = 8


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


def device_id_for(tenant_id: TenantId, hardware_id: str) -> DeviceId:
    """A stable id for a device of the file: the same file always gives the same devices the same
    ids, which is what makes a conversation on a device carry on from one start to the next."""
    return DeviceId(uuid5(NAMESPACE_URL, f"urn:factotum:device:{tenant_id}:{hardware_id}"))


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


_KNOWN = {
    "id",
    "name",
    "hardware_id",
    "token",
    "owner",
    "capabilities",
    "display",
    "tags",
    "aliases",
}


def load_devices(
    text: str,
    settings: ServiceSettings,
    *,
    tenant_id: TenantId,
    user_id: UserId,
    source: str = "devices",
) -> FileDeviceRegistry:
    """The registry described by a YAML document.

    ``devices`` is a list; each entry has a ``name``, a ``hardware_id``, a ``token`` (the
    credential the device presents), an ``owner`` (``tenant``, for a shared device, or ``user``,
    for one that is the development user's own) and the ``capabilities`` it is registered for,
    and optionally a ``display``, ``tags``, ``aliases`` and an ``id``.
    """
    document = yaml.safe_load(text)
    if not isinstance(document, dict) or not isinstance(document.get("devices"), list):
        raise ValueError(f"{source}: expected a mapping with a 'devices' list")
    unknown = set(document) - {"devices"}
    if unknown:
        raise ValueError(f"{source}: unknown top-level keys: {sorted(unknown)}")

    loaded: list[tuple[SystemDeviceRecord, str]] = []
    for index, entry in enumerate(document["devices"], start=1):
        where = f"{source}: device #{index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected a mapping")
        loaded.append(_device_from(entry, tenant_id, user_id, where))

    for seen, label in (
        ({record.hardware_id for record, _ in loaded}, "hardware id"),
        ({token for _, token in loaded}, "token"),
        ({record.id for record, _ in loaded}, "id"),
    ):
        if len(seen) != len(loaded):
            raise ValueError(f"{source}: two devices have the same {label}")
    return FileDeviceRegistry(settings, loaded)


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


def _device_from(
    entry: dict[str, Any], tenant_id: TenantId, user_id: UserId, where: str
) -> tuple[SystemDeviceRecord, str]:
    extra = set(entry) - _KNOWN
    if extra:
        raise ValueError(f"{where}: unknown keys: {sorted(extra)}")
    token = entry.get("token")
    if not isinstance(token, str) or len(token) < MIN_TOKEN_CHARS:
        raise ValueError(
            f"{where}: 'token' must be a string of at least {MIN_TOKEN_CHARS} characters"
        )
    owner_kind = entry.get("owner", "tenant")
    if owner_kind not in {"tenant", "user"}:
        raise ValueError(f"{where}: 'owner' must be 'tenant' or 'user'")
    subject = Subject.user(user_id) if owner_kind == "user" else Subject.tenant(tenant_id)
    hardware_id = entry.get("hardware_id")
    if not isinstance(hardware_id, str):
        raise ValueError(f"{where}: 'hardware_id' is required")
    try:
        identity = entry.get("id")
        device_id = (
            DeviceId(UUID(str(identity))) if identity else device_id_for(tenant_id, hardware_id)
        )
        display = entry.get("display")
        record = SystemDeviceRecord.model_validate(
            {
                "id": device_id,
                "tenant_id": tenant_id,
                "name": entry.get("name"),
                "hardware_id": hardware_id,
                "owner": Owner.of_subject(subject),
                "policy": DevicePolicy.default_for(subject.kind),
                "capabilities": frozenset(
                    Capability(name) for name in entry.get("capabilities", [])
                ),
                "display": DisplayInfo.model_validate(display) if display else None,
                "tags": frozenset(entry.get("tags", [])),
                "aliases": tuple(entry.get("aliases", [])),
            }
        )
    except (ValidationError, ValueError, TypeError) as error:
        raise ValueError(f"{where}: {_why(error)}") from None
    return record, token


def _why(error: Exception) -> str:
    """What is wrong, never what was written: it may be a credential in the wrong place."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in problem['loc']) or '(entry)'}: {problem['msg']}"
            for problem in error.errors(
                include_input=False, include_context=False, include_url=False
            )
        )
    return "a value is not valid"
