"""Typed errors shared by the services.

Callers react to the *kind* of error, never to a provider- or transport-specific detail. Each error
carries a stable machine-readable ``code`` so it can cross an HTTP or WebSocket boundary.
"""

from collections.abc import Mapping
from typing import Any, ClassVar


class FactotumError(Exception):
    """Base class of every error the services raise on purpose."""

    code: ClassVar[str] = "factotum_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class AuthError(FactotumError):
    """The caller could not be authenticated."""

    code = "auth_error"


class PolicyViolationError(FactotumError):
    """The request is refused by a policy (tenant, device, data locality...)."""

    code = "policy_violation"


class UrlNotAllowedError(FactotumError):
    """An outbound URL is refused by the SSRF guard."""

    code = "url_not_allowed"


class CapabilityNotSupportedError(FactotumError):
    """A device was asked to do something it did not declare it can do."""

    code = "capability_not_supported"
