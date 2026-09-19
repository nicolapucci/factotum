"""Builders for tests: small, valid instances of the contracts with sensible defaults.

Shipping them next to the contracts keeps every service's tests short and consistent.
"""

from uuid import uuid4

from factotum.contracts.identity import Principal, Role, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId


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
