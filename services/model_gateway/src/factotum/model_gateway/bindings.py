"""Which model serves a role, for whom.

A user may bind a role to a model of their own; the tenant may bind it for everybody. The user's
choice wins over the tenant's, so a person who brings their own subscription is served by it. A
device that acts for the tenant, like a speaker in the living room, is served by the tenant's.

Once the hub registers bindings (slice 7c), ``HubBindingRepository`` fetches its tenant's once, in
the lifespan. Until a token secret is configured this is a file, for development (the gateway still
never holds a provider's key for them: a binding only ever names an environment variable, resolved
the same way whichever source the binding came from).
"""

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
import yaml
from pydantic import ValidationError

from factotum.common.devauth import PrincipalDelegation
from factotum.contracts.errors import BindingNotFoundError
from factotum.contracts.hub import ModelBindingList, ModelBindingView
from factotum.contracts.identity import Owner, OwnerKind, Principal, Subject, SubjectKind
from factotum.contracts.ids import ModelBindingId, TenantId, UserId
from factotum.contracts.models import ModelBinding, ModelRole


class BindingRepository(Protocol):
    async def find(self, tenant_id: TenantId, owner: Owner, role: ModelRole) -> ModelBinding | None:
        """The binding of ``role`` that ``owner`` made in the tenant, if there is one."""
        ...


class InMemoryBindingRepository:
    """Bindings held in memory: for development and tests, and for the file loader below."""

    def __init__(self, bindings: Iterable[ModelBinding] = ()) -> None:
        self._bindings: dict[tuple[TenantId, OwnerKind, UUID, ModelRole], ModelBinding] = {}
        for binding in bindings:
            self.add(binding)

    def add(self, binding: ModelBinding) -> None:
        key = (binding.tenant_id, binding.owner.kind, binding.owner.id, binding.role)
        if key in self._bindings:
            raise ValueError(
                f"{binding.owner.kind.value} {binding.owner.id} already has a binding for "
                f"{binding.role.value}"
            )
        self._bindings[key] = binding

    async def find(self, tenant_id: TenantId, owner: Owner, role: ModelRole) -> ModelBinding | None:
        return self._bindings.get((tenant_id, owner.kind, owner.id, role))

    def __len__(self) -> int:
        return len(self._bindings)


def fallback_roles(role: ModelRole) -> tuple[ModelRole, ...]:
    """The roles that may serve ``role``, the role itself first. A fast model is a convenience:
    without one, the main model does its work."""
    if role is ModelRole.CHAT_FAST:
        return (ModelRole.CHAT_FAST, ModelRole.CHAT_MAIN)
    return (role,)


def owners_for(tenant_id: TenantId, subject: Subject) -> tuple[Owner, ...]:
    """Whose bindings apply to ``subject``, the most specific first."""
    tenant = Owner.of_subject(Subject.tenant(tenant_id))
    if subject.kind is SubjectKind.USER:
        return (Owner.of_subject(subject), tenant)
    return (tenant,)


async def select_binding(
    repository: BindingRepository, tenant_id: TenantId, subject: Subject, role: ModelRole
) -> ModelBinding:
    """The binding that serves ``role`` for ``subject``.

    A binding for the role itself wins wherever it is: the user's own, else the tenant's. Only
    when nobody bound the role does the fallback role apply, so a user who bound just their main
    model does not shadow a fast model the tenant chose.
    """
    owners = owners_for(tenant_id, subject)
    for candidate in fallback_roles(role):
        for owner in owners:
            binding = await repository.find(tenant_id, owner, candidate)
            if binding is not None:
                return binding
    raise BindingNotFoundError(f"no model is bound to {role.value}", details={"role": role.value})


def load_bindings(
    text: str, *, tenant_id: TenantId, source: str = "bindings"
) -> list[ModelBinding]:
    """Bindings from a YAML document, for development before the hub manages them.

    ``bindings`` is a list; each entry has the fields of a binding (``role``, ``provider``,
    ``model``, ``base_url``, ``secret_ref``, ``options``). It belongs to the tenant,
    unless it has a ``user`` id: then it is that user's own.
    """
    document = yaml.safe_load(text)
    if not isinstance(document, dict) or not isinstance(document.get("bindings"), list):
        raise ValueError(f"{source}: expected a mapping with a 'bindings' list")
    unknown = set(document) - {"bindings"}
    if unknown:
        raise ValueError(f"{source}: unknown top-level keys: {sorted(unknown)}")

    loaded: list[ModelBinding] = []
    for index, entry in enumerate(document["bindings"], start=1):
        where = f"{source}: binding #{index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected a mapping")
        loaded.append(_binding_from(entry, tenant_id, where))
    return loaded


def load_bindings_file(path: Path, *, tenant_id: TenantId) -> list[ModelBinding]:
    return load_bindings(path.read_text(encoding="utf-8"), tenant_id=tenant_id, source=path.name)


def _binding_from(entry: dict[str, Any], tenant_id: TenantId, where: str) -> ModelBinding:
    fields = dict(entry)
    user = fields.pop("user", None)
    try:
        subject = Subject.user(UserId(UUID(str(user)))) if user else Subject.tenant(tenant_id)
    except ValueError:
        raise ValueError(f"{where}: 'user' is not a valid id") from None
    try:
        return ModelBinding.model_validate(
            {
                **fields,
                "id": ModelBindingId(uuid4()),
                "tenant_id": tenant_id,
                "owner": Owner.of_subject(subject),
            }
        )
    except ValidationError as error:
        # Only where and why: the value may be a key pasted where a reference belongs.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in problem['loc']) or '(entry)'}: {problem['msg']}"
            for problem in error.errors(
                include_input=False, include_context=False, include_url=False
            )
        )
        raise ValueError(f"{where}: {problems}") from None


def _binding_from_view(view: ModelBindingView, tenant_id: TenantId) -> ModelBinding:
    return ModelBinding(
        id=view.id,
        tenant_id=tenant_id,
        owner=view.owner,
        role=view.role,
        provider=view.provider,
        model=view.model,
        base_url=view.base_url,
        secret_ref=view.secret_ref,
        options=view.options,
    )


class HubBindingRepository:
    """The model bindings of the hub's tenant: empty until ``reload`` is awaited once, in the
    lifespan, before the gateway starts serving."""

    def __init__(
        self, client: httpx.AsyncClient, delegation: PrincipalDelegation, tenant_id: TenantId
    ) -> None:
        self._client = client
        self._delegation = delegation
        self._tenant_id = tenant_id
        self._bindings: dict[tuple[OwnerKind, UUID, ModelRole], ModelBinding] = {}

    async def reload(self) -> None:
        headers: Mapping[str, str] = self._delegation.headers(Principal.of_tenant(self._tenant_id))
        response = await self._client.get("/v1/models", headers=headers)
        response.raise_for_status()
        bindings = ModelBindingList.model_validate_json(response.content)
        self._bindings = {
            (view.owner.kind, view.owner.id, view.role): _binding_from_view(view, self._tenant_id)
            for view in bindings.bindings
        }

    async def find(self, tenant_id: TenantId, owner: Owner, role: ModelRole) -> ModelBinding | None:
        if tenant_id != self._tenant_id:
            return None
        return self._bindings.get((owner.kind, owner.id, role))
