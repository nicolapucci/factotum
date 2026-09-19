"""Builders for tests: small, valid instances of the contracts with sensible defaults.

Shipping them next to the contracts keeps every service's tests short and consistent.
"""

from collections.abc import Iterable
from uuid import uuid4

from factotum.contracts.authorization import SkillRef, TargetRef
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.devices import DevicePolicy, ExternalDeviceRecord, SystemDeviceRecord
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, OwnerKind, Principal, Role, Subject, SubjectKind
from factotum.contracts.ids import DeviceId, ModelBindingId, SkillId, TenantId, UserId
from factotum.contracts.models import (
    BindingOptions,
    Locality,
    ModelBinding,
    ModelRole,
    ProviderKind,
)
from factotum.contracts.skills import ApprovalPolicy, ConfirmPolicy


def _default_policy(owner: Owner) -> DevicePolicy:
    # An invalid owner (a device) still gets a policy, so the contract itself can refuse it.
    kind = SubjectKind.USER if owner.kind is OwnerKind.USER else SubjectKind.TENANT
    return DevicePolicy.default_for(kind)


def make_tenant_id() -> TenantId:
    return TenantId(uuid4())


def make_user_id() -> UserId:
    return UserId(uuid4())


def make_device_id() -> DeviceId:
    return DeviceId(uuid4())


def make_user_principal(
    *,
    tenant_id: TenantId | None = None,
    user_id: UserId | None = None,
    roles: frozenset[Role] = frozenset({Role.MEMBER}),
    device_id: DeviceId | None = None,
) -> Principal:
    """A principal acting as a user (a personal device, or a hub session)."""
    return Principal(
        tenant_id=tenant_id or make_tenant_id(),
        device_id=device_id,
        acts_as=Subject.user(user_id or make_user_id()),
        roles=roles,
    )


def make_tenant_principal(
    *, tenant_id: TenantId | None = None, device_id: DeviceId | None = None
) -> Principal:
    """A principal acting as the tenant (a communal device)."""
    tenant = tenant_id or make_tenant_id()
    return Principal(tenant_id=tenant, device_id=device_id, acts_as=Subject.tenant(tenant))


def make_system_device(
    *,
    tenant_id: TenantId | None = None,
    owner: Owner | None = None,
    capabilities: Iterable[Capability] = (Capability.AUDIO_IN, Capability.AUDIO_OUT),
    display: DisplayInfo | None = None,
    hardware_id: str = "AA:BB:CC:DD:EE:FF",
    name: str = "Kitchen speaker",
    policy: DevicePolicy | None = None,
    device_id: DeviceId | None = None,
) -> SystemDeviceRecord:
    """A system device, personal to a fresh user unless an owner is given."""
    tenant = tenant_id or make_tenant_id()
    device_owner = owner or Owner.of_subject(Subject.user(make_user_id()))
    return SystemDeviceRecord(
        id=device_id or make_device_id(),
        tenant_id=tenant,
        name=name,
        owner=device_owner,
        policy=policy or _default_policy(device_owner),
        hardware_id=hardware_id,
        capabilities=frozenset(capabilities),
        display=display,
    )


def make_external_device(
    *,
    tenant_id: TenantId | None = None,
    owner: Owner | None = None,
    name: str = "Kitchen light",
    sensitivity: Effect = Effect.READ_ONLY,
    device_id: DeviceId | None = None,
) -> ExternalDeviceRecord:
    """An external device owned by the tenant unless an owner is given."""
    tenant = tenant_id or make_tenant_id()
    device_owner = owner or Owner.of_subject(Subject.tenant(tenant))
    return ExternalDeviceRecord(
        id=device_id or make_device_id(),
        tenant_id=tenant,
        name=name,
        owner=device_owner,
        policy=_default_policy(device_owner),
        sensitivity=sensitivity,
    )


def make_skill_ref(
    *,
    tenant_id: TenantId,
    owner: Owner | None = None,
    effect: Effect = Effect.REVERSIBLE,
    confirm: ConfirmPolicy = ConfirmPolicy.NEVER,
    approval: ApprovalPolicy = ApprovalPolicy.NONE,
) -> SkillRef:
    """A skill reference, owned by the tenant unless an owner is given."""
    return SkillRef(
        id=SkillId(uuid4()),
        tenant_id=tenant_id,
        owner=owner or Owner.of_subject(Subject.tenant(tenant_id)),
        effect=effect,
        confirm=confirm,
        approval=approval,
    )


def make_target_ref(
    *,
    tenant_id: TenantId,
    owner: Owner | None = None,
    sensitivity: Effect = Effect.READ_ONLY,
) -> TargetRef:
    """A target device reference, owned by the tenant unless an owner is given."""
    return TargetRef(
        device_id=make_device_id(),
        tenant_id=tenant_id,
        owner=owner or Owner.of_subject(Subject.tenant(tenant_id)),
        sensitivity=sensitivity,
    )


def make_model_binding(
    *,
    tenant_id: TenantId,
    role: ModelRole = ModelRole.CHAT_MAIN,
    provider: ProviderKind = ProviderKind.OLLAMA,
    model: str = "llama3.1:8b",
    base_url: str | None = "http://localhost:11434",
    secret_ref: str | None = None,
    owner: Owner | None = None,
    locality: Locality | None = None,
    options: BindingOptions | None = None,
) -> ModelBinding:
    """A model binding, owned by the tenant unless an owner is given; a local Ollama by default."""
    return ModelBinding(
        id=ModelBindingId(uuid4()),
        tenant_id=tenant_id,
        owner=owner or Owner.of_subject(Subject.tenant(tenant_id)),
        role=role,
        provider=provider,
        model=model,
        base_url=base_url,
        secret_ref=secret_ref,
        locality=locality,
        options=options or BindingOptions(),
    )
