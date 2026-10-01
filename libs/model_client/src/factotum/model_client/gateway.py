"""What every call to the model gateway (and the knowledge service, which the same delegation
proves an owner to) has in common: acting for an owner, and reading the gateway's errors.

Acting for the owner is delegation. Once the hub issues service credentials
(``FACTOTUM_HUB_TOKEN_SECRET``), ``HubDelegation`` mints a signed token of its own for the owner,
the same way ``HubPrincipalDelegation`` mints one for a ``Principal``
(``factotum.common.hubauth``); until then, the development delegation is what the gateway's own
development authentication understands.
"""

from collections.abc import Mapping
from typing import Protocol

import httpx

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import SUBJECT_HEADER
from factotum.common.hubauth import DELEGATION_TTL_S
from factotum.common.tokens import encode_token
from factotum.contracts.errors import (
    AuthError,
    BindingNotFoundError,
    ChatNotConfiguredError,
    EmbeddingNotConfiguredError,
    FactotumError,
    GatewayError,
    GatewayUnavailableError,
    RateLimitedError,
)
from factotum.contracts.identity import Owner, OwnerKind, Principal, Role, Subject
from factotum.contracts.ids import TenantId, UserId
from factotum.contracts.models import ModelRole


class Delegation(Protocol):
    def headers(self, owner: Owner) -> Mapping[str, str]:
        """What proves to the gateway that this service acts for ``owner``."""
        ...


class DevDelegation:
    """The development delegation: the gateway's development authentication takes the subject from
    a header. It refuses to exist outside development and tests."""

    def __init__(self, settings: ServiceSettings) -> None:
        if settings.environment not in {Environment.DEVELOPMENT, Environment.TEST}:
            raise AuthError(
                "the development delegation is disabled outside the development and test "
                f"environments (environment is {settings.environment.value!r})"
            )

    def headers(self, owner: Owner) -> Mapping[str, str]:
        if owner.kind is OwnerKind.USER:
            return {SUBJECT_HEADER: "user"}
        if owner.kind is OwnerKind.TENANT:
            return {SUBJECT_HEADER: "tenant"}
        raise ValueError("the gateway is asked for by a user or by the tenant")


class HubDelegation:
    """The hub's delegation, for one fixed tenant: ``Owner`` alone does not carry a tenant (a
    user's id is not enough to say whose user it is), so unlike ``HubPrincipalDelegation``, which
    re-signs a ``Principal`` it is already given, this one is built knowing which tenant it mints
    for. Fine today, since it is only ever built for ``SINGLE_TENANT_ID``; a deployment with more
    than one tenant needs this threaded through instead, a larger change than this makes.

    Works in every environment: unlike the development delegation, it does not need one of its own.
    """

    def __init__(
        self, secret: bytes, tenant_id: TenantId, *, ttl_s: int = DELEGATION_TTL_S
    ) -> None:
        self._secret = secret
        self._tenant_id = tenant_id
        self._ttl_s = ttl_s

    def headers(self, owner: Owner) -> Mapping[str, str]:
        if owner.kind is OwnerKind.USER:
            # A role is required of a user subject, but the gateway reads none: the least one.
            principal = Principal(
                tenant_id=self._tenant_id,
                acts_as=Subject.user(UserId(owner.id)),
                roles=frozenset({Role.MEMBER}),
            )
        elif owner.kind is OwnerKind.TENANT:
            principal = Principal.of_tenant(self._tenant_id)
        else:
            raise ValueError("the gateway is asked for by a user or by the tenant")
        token = encode_token(principal, secret=self._secret, ttl_s=self._ttl_s)
        return {"authorization": f"Bearer {token}"}


def delegation_for(settings: ServiceSettings, tenant_id: TenantId) -> DevDelegation | HubDelegation:
    """The real delegation once a token secret is configured; otherwise the development one, which
    does not exist outside development and test, so a service still refuses to start in production
    until a secret is set."""
    if settings.hub_secret_bytes is not None:
        return HubDelegation(settings.hub_secret_bytes, tenant_id)
    return DevDelegation(settings)


def _error_classes() -> dict[str, type[FactotumError]]:
    found: dict[str, type[FactotumError]] = {}
    pending: list[type[FactotumError]] = [FactotumError]
    while pending:
        kind = pending.pop()
        found.setdefault(kind.code, kind)
        pending.extend(kind.__subclasses__())
    return found


def error_from(response: httpx.Response, role: ModelRole = ModelRole.EMBEDDING) -> FactotumError:
    """The typed error the gateway answered with. The kind is in the ``code``; a body that has none
    (a proxy's page, an outage) is judged by its status. ``role`` is what was asked for, since
    "there is no model for it" is a different thing to tell an embedding from a chat."""
    code = message = None
    details: Mapping[str, object] = {}
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
        details = error.get("details") or {}
    except (ValueError, KeyError, TypeError):
        pass

    unavailable = response.status_code in {502, 503, 504} and code is None
    if unavailable:
        return GatewayUnavailableError("the model gateway is not answering")
    if code is None or message is None:
        return GatewayError(f"the model gateway answered with an error ({response.status_code})")

    if code == RateLimitedError.code:
        retry_after = response.headers.get("Retry-After", "")
        return RateLimitedError(
            message, retry_after_s=float(retry_after) if retry_after.isdigit() else None
        )
    if code == BindingNotFoundError.code:
        if role is ModelRole.EMBEDDING:
            return EmbeddingNotConfiguredError("no embedding model is set up for the owner")
        return ChatNotConfiguredError("no chat model is set up for the owner")
    kind = _error_classes().get(code, GatewayError)
    return kind(message, details=details)
