"""Who may do what with a collection.

**Seeing and searching** follows ownership (``can_access``): a collection of the tenant is there for
everybody in it, a collection of a user only for that user. A collection somebody may not see does
not exist for them: asking for it is answered as if it were not there.

**Changing** (creating, adding and deleting documents, re-indexing, deleting) is administration,
and administration happens through a person's own session, not through a device: a shared device
(which acts as the tenant) can search but never change. A user changes their own collections; the
collections of the tenant are changed by its administrators. Documents are somebody else's words
that end up in front of a model, so who may put them in front of everybody is not left open.
"""

from factotum.common.authorization import can_access
from factotum.contracts.errors import CollectionNotFoundError, PolicyViolationError
from factotum.contracts.identity import Owner, OwnerKind, Principal, Role, SubjectKind
from factotum.contracts.knowledge import Collection, CollectionOwner


def can_read(principal: Principal, collection: Collection) -> bool:
    return can_access(principal, resource_tenant_id=collection.tenant_id, owner=collection.owner)


def can_manage(principal: Principal, collection: Collection) -> bool:
    if not can_read(principal, collection):
        return False
    if principal.acts_as.kind is not SubjectKind.USER:
        return False
    # A user's own collection (``can_read`` made sure it is theirs), or the tenant's, which is for
    # its administrators. What belongs to a device is not changed from here.
    owner = collection.owner.kind
    return owner is OwnerKind.USER or (owner is OwnerKind.TENANT and Role.ADMIN in principal.roles)


def owner_for(principal: Principal, kind: CollectionOwner) -> Owner:
    """The owner of a collection the principal is creating. Raises ``PolicyViolationError``."""
    if principal.acts_as.kind is not SubjectKind.USER:
        raise PolicyViolationError(
            "a shared device can search collections, but only a person can create one"
        )
    if kind is CollectionOwner.TENANT:
        if Role.ADMIN not in principal.roles:
            raise PolicyViolationError(
                "only an administrator can create a collection for everybody in the tenant"
            )
        return Owner(kind=OwnerKind.TENANT, id=principal.tenant_id)
    return Owner.of_subject(principal.acts_as)


def readable(principal: Principal, collection: Collection) -> Collection:
    """The collection, if the principal may see it; otherwise it is not there for them."""
    if not can_read(principal, collection):
        raise CollectionNotFoundError("the collection does not exist")
    return collection


def manageable(principal: Principal, collection: Collection) -> Collection:
    """The collection, if the principal may change it. One they may see but not change is refused
    with the reason; one they may not see is not there."""
    readable(principal, collection)
    if not can_manage(principal, collection):
        raise PolicyViolationError("you may search this collection but not change it")
    return collection
