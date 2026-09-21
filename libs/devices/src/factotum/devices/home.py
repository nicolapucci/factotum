"""The devices of a home, as one development file describes them.

Until the hub exists (slice 7) the devices are described in a YAML file, for development. It has

* the **system devices**, the ones that connect to the edge: a hardware id, the credential they
  present, and what they are registered to do;
* the **external devices**, a lamp or a garage door: no credential and nothing installed on them,
  only what they are, whom they belong to and how sensitive they are;
* the **bindings**, which say which skill drives which external device, and with which parameters
  of the device's own.

The edge reads the system devices from it, to know who may connect, and the actions service reads
all of it, to know what may be acted on. Neither is to run outside development: a file is not how a
device is registered in production, and a credential in it is a placeholder.

Nothing here says what was written when something is wrong (it may be a credential in the wrong
place): it says where, and why.
"""

from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import ValidationError

from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.devices import (
    DevicePolicy,
    ExternalDeviceRecord,
    JsonScalar,
    SystemDeviceRecord,
)
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId

MIN_TOKEN_CHARS = 8


def device_id_for(tenant_id: TenantId, hardware_id: str) -> DeviceId:
    """A stable id for a system device of the file: the same file always gives the same devices the
    same ids, which is what makes a conversation on a device carry on from one start to the next."""
    return DeviceId(uuid5(NAMESPACE_URL, f"urn:factotum:device:{tenant_id}:{hardware_id}"))


def external_device_id_for(tenant_id: TenantId, name: str) -> DeviceId:
    """The same for an external device, which is known by its name."""
    return DeviceId(uuid5(NAMESPACE_URL, f"urn:factotum:external-device:{tenant_id}:{name}"))


@dataclass(frozen=True, slots=True)
class BindingSpec:
    """A skill bound to an external device, with the device's own parameters. The skill is known by
    its name: it is installed apart from the file."""

    skill: str
    device_id: DeviceId
    device_name: str
    params: dict[str, JsonScalar]
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Home:
    system: tuple[tuple[SystemDeviceRecord, str], ...] = ()
    """The system devices, each with the credential it presents."""

    external: tuple[ExternalDeviceRecord, ...] = ()
    bindings: tuple[BindingSpec, ...] = ()


_SYSTEM_KEYS = {
    "kind",
    "id",
    "name",
    "hardware_id",
    "token",
    "owner",
    "capabilities",
    "display",
    "tags",
    "aliases",
    "policy",
}
_EXTERNAL_KEYS = {
    "kind",
    "id",
    "name",
    "owner",
    "tags",
    "aliases",
    "sensitivity",
    "external_refs",
    "policy",
}
_POLICY_KEYS = {"max_effect", "requires_approval"}
_BINDING_KEYS = {"skill", "device", "params", "enabled"}


def parse_home(text: str, *, tenant_id: TenantId, user_id: UserId, source: str = "devices") -> Home:
    """The home described by a YAML document.

    ``devices`` is a list. A system device (the default ``kind``) has a ``name``, a ``hardware_id``,
    a ``token`` (the credential it presents), an ``owner`` (``tenant``, for a shared device, or
    ``user``, for one that is the development user's own) and the ``capabilities`` it is registered
    for, and optionally a ``display``, ``tags``, ``aliases``, a ``policy`` and an ``id``. An
    external device (``kind: external``) has a ``name``, an ``owner``, and optionally ``aliases``,
    ``tags``, a ``sensitivity`` (how much more severe than the skill's own effect an action on it
    is), ``external_refs``, a ``policy`` and an ``id``. ``bindings`` is a list of ``skill``,
    ``device`` (the name of an external device), ``params`` and ``enabled``.
    """
    document = yaml.safe_load(text)
    if not isinstance(document, dict) or not isinstance(document.get("devices"), list):
        raise ValueError(f"{source}: expected a mapping with a 'devices' list")
    unknown = set(document) - {"devices", "bindings"}
    if unknown:
        raise ValueError(f"{source}: unknown top-level keys: {sorted(unknown)}")

    system: list[tuple[SystemDeviceRecord, str]] = []
    external: list[ExternalDeviceRecord] = []
    for index, entry in enumerate(document["devices"], start=1):
        where = f"{source}: device #{index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected a mapping")
        kind = entry.get("kind", "system")
        if kind == "system":
            system.append(_system_device(entry, tenant_id, user_id, where))
        elif kind == "external":
            external.append(_external_device(entry, tenant_id, user_id, where))
        else:
            raise ValueError(f"{where}: 'kind' must be 'system' or 'external'")

    for seen, total, label in (
        ({record.hardware_id for record, _ in system}, len(system), "hardware id"),
        ({token for _, token in system}, len(system), "token"),
        ({record.name for record in external}, len(external), "name"),
        (
            {record.id for record, _ in system} | {record.id for record in external},
            len(system) + len(external),
            "id",
        ),
    ):
        if len(seen) != total:
            raise ValueError(f"{source}: two devices have the same {label}")

    bindings = _bindings(document.get("bindings"), external, source)
    return Home(system=tuple(system), external=tuple(external), bindings=bindings)


def _subject(entry: dict[str, Any], tenant_id: TenantId, user_id: UserId, where: str) -> Subject:
    owner_kind = entry.get("owner", "tenant")
    if owner_kind not in {"tenant", "user"}:
        raise ValueError(f"{where}: 'owner' must be 'tenant' or 'user'")
    return Subject.user(user_id) if owner_kind == "user" else Subject.tenant(tenant_id)


def _policy(entry: dict[str, Any], subject: Subject, where: str) -> DevicePolicy:
    default = DevicePolicy.default_for(subject.kind)
    written = entry.get("policy")
    if written is None:
        return default
    if not isinstance(written, dict):
        raise ValueError(f"{where}: 'policy' must be a mapping")
    unknown = set(written) - _POLICY_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown policy keys: {sorted(unknown)}")
    requires_approval = written.get("requires_approval", default.requires_approval)
    if not isinstance(requires_approval, bool):
        raise ValueError(f"{where}: 'requires_approval' must be true or false")
    try:
        max_effect = Effect(written.get("max_effect", default.max_effect))
    except ValueError:
        raise ValueError(
            f"{where}: 'max_effect' must be one of {[effect.value for effect in Effect]}"
        ) from None
    return DevicePolicy(max_effect=max_effect, requires_approval=requires_approval)


def _system_device(
    entry: dict[str, Any], tenant_id: TenantId, user_id: UserId, where: str
) -> tuple[SystemDeviceRecord, str]:
    extra = set(entry) - _SYSTEM_KEYS
    if extra:
        raise ValueError(f"{where}: unknown keys: {sorted(extra)}")
    token = entry.get("token")
    if not isinstance(token, str) or len(token) < MIN_TOKEN_CHARS:
        raise ValueError(
            f"{where}: 'token' must be a string of at least {MIN_TOKEN_CHARS} characters"
        )
    subject = _subject(entry, tenant_id, user_id, where)
    hardware_id = entry.get("hardware_id")
    if not isinstance(hardware_id, str):
        raise ValueError(f"{where}: 'hardware_id' is required")
    policy = _policy(entry, subject, where)
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
                "policy": policy,
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


def _external_device(
    entry: dict[str, Any], tenant_id: TenantId, user_id: UserId, where: str
) -> ExternalDeviceRecord:
    extra = set(entry) - _EXTERNAL_KEYS
    if extra:
        raise ValueError(f"{where}: unknown keys: {sorted(extra)}")
    subject = _subject(entry, tenant_id, user_id, where)
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where}: 'name' is required")
    policy = _policy(entry, subject, where)
    try:
        sensitivity = Effect(entry.get("sensitivity", Effect.READ_ONLY))
    except ValueError:
        raise ValueError(
            f"{where}: 'sensitivity' must be one of {[effect.value for effect in Effect]}"
        ) from None
    refs = entry.get("external_refs", {})
    if not isinstance(refs, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in refs.items()
    ):
        raise ValueError(f"{where}: 'external_refs' must map names to strings")
    try:
        identity = entry.get("id")
        device_id = (
            DeviceId(UUID(str(identity)))
            if identity
            else external_device_id_for(tenant_id, name.strip())
        )
        return ExternalDeviceRecord.model_validate(
            {
                "id": device_id,
                "tenant_id": tenant_id,
                "name": name,
                "owner": Owner.of_subject(subject),
                "policy": policy,
                "tags": frozenset(entry.get("tags", [])),
                "aliases": tuple(entry.get("aliases", [])),
                "sensitivity": sensitivity,
                "external_refs": refs,
            }
        )
    except (ValidationError, ValueError, TypeError) as error:
        raise ValueError(f"{where}: {_why(error)}") from None


def _bindings(
    written: Any, external: list[ExternalDeviceRecord], source: str
) -> tuple[BindingSpec, ...]:
    if written is None:
        return ()
    if not isinstance(written, list):
        raise ValueError(f"{source}: 'bindings' must be a list")
    by_name = {record.name: record for record in external}
    bindings: list[BindingSpec] = []
    for index, entry in enumerate(written, start=1):
        where = f"{source}: binding #{index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected a mapping")
        extra = set(entry) - _BINDING_KEYS
        if extra:
            raise ValueError(f"{where}: unknown keys: {sorted(extra)}")
        skill = entry.get("skill")
        if not isinstance(skill, str) or not skill:
            raise ValueError(f"{where}: 'skill' is required")
        named = entry.get("device")
        device = by_name.get(named) if isinstance(named, str) else None
        if device is None:
            raise ValueError(f"{where}: 'device' must be the name of an external device")
        params = entry.get("params", {})
        if not isinstance(params, dict) or not all(
            isinstance(key, str) and isinstance(value, str | int | float | bool)
            for key, value in params.items()
        ):
            raise ValueError(f"{where}: 'params' must map names to text, numbers or booleans")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{where}: 'enabled' must be true or false")
        bindings.append(
            BindingSpec(
                skill=skill,
                device_id=device.id,
                device_name=device.name,
                params=dict(params),
                enabled=enabled,
            )
        )
    twice = {(b.skill, b.device_id) for b in bindings}
    if len(twice) != len(bindings):
        raise ValueError(f"{source}: a skill is bound to the same device twice")
    return tuple(bindings)


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
