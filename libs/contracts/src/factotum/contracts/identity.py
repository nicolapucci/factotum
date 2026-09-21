"""Who is acting and who owns what: tenants, users, subjects, principals, owners.

A *tenant* is a household or an organization and is the isolation boundary. A *user* is a person
inside a tenant. A *subject* is whoever a device acts on behalf of: a user or the tenant itself.
The tenant is a subject of the same standing as a user, so permissions follow one procedure for
both (see ``factotum.common.authorization``).
"""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import model_validator

from factotum.contracts.base import Contract
from factotum.contracts.ids import DeviceId, TenantId, UserId


class TenantMode(StrEnum):
    """How a deployment is organized. On-premise is simply a deployment with a single tenant."""

    SINGLE_TENANT = "single_tenant"
    MULTI_TENANT = "multi_tenant"


class Role(StrEnum):
    """What a user may do inside a tenant. Administration happens only through authenticated
    hub sessions, never through a device."""

    ADMIN = "admin"
    MEMBER = "member"


class SubjectKind(StrEnum):
    USER = "user"
    TENANT = "tenant"


class Subject(Contract):
    """Whoever a device acts on behalf of: a user or the tenant."""

    kind: SubjectKind
    id: UUID

    @classmethod
    def user(cls, user_id: UserId) -> Self:
        return cls(kind=SubjectKind.USER, id=user_id)

    @classmethod
    def tenant(cls, tenant_id: TenantId) -> Self:
        return cls(kind=SubjectKind.TENANT, id=tenant_id)


class Principal(Contract):
    """The authenticated context of a request.

    ``acts_as`` is derived from the device that made the request (a personal device acts as its
    owner, a communal device acts as the tenant) or from the logged-in user in a hub session.
    Nothing else, in particular nothing inferred from a voice, takes part in permission decisions.
    """

    tenant_id: TenantId
    device_id: DeviceId | None = None
    acts_as: Subject
    roles: frozenset[Role] = frozenset()

    @model_validator(mode="after")
    def _check_subject_and_roles(self) -> Self:
        if self.acts_as.kind is SubjectKind.TENANT:
            if self.acts_as.id != self.tenant_id:
                raise ValueError("a tenant subject must be the principal's own tenant")
            if self.roles:
                raise ValueError("a tenant subject has no roles")
        elif not self.roles:
            raise ValueError("a user subject needs at least one role")
        return self

    @classmethod
    def of_tenant(cls, tenant_id: TenantId) -> Self:
        """The tenant acting as itself: a service that acts for the household, not for a person or
        for the device that spoke (the actions service, when it has a device do something)."""
        return cls(tenant_id=tenant_id, acts_as=Subject.tenant(tenant_id))


class OwnerKind(StrEnum):
    USER = "user"
    TENANT = "tenant"
    DEVICE = "device"


class Owner(Contract):
    """Who owns a resource (a document collection, a skill, a linked account, a device...).

    The owner decides who may see and use it: the tenant shares it with everyone in the tenant, a
    user keeps it private, a device keeps it for itself (the TV's own streaming account).
    """

    kind: OwnerKind
    id: UUID

    @classmethod
    def of_subject(cls, subject: Subject) -> Self:
        return cls(kind=OwnerKind(subject.kind.value), id=subject.id)

    @classmethod
    def of_device(cls, device_id: DeviceId) -> Self:
        return cls(kind=OwnerKind.DEVICE, id=device_id)
