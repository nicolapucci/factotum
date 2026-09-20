"""What every call to the model gateway has in common: acting for an owner, and reading its errors.

Acting for the owner is delegation: the gateway holds the owner's bindings. Until the hub issues
service credentials (Slice 7) the only delegation is the development one, which the gateway's own
development authentication understands.
"""

from collections.abc import Mapping
from typing import Protocol

import httpx

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import SUBJECT_HEADER
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
from factotum.contracts.identity import Owner, OwnerKind
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
