"""Real authentication, backed by a token the hub signed.

``HubAuthenticator`` and ``HubPrincipalDelegation`` are what a service uses once a token secret is
configured, in place of the development ones (``factotum.common.devauth``) that refuse to exist
outside development and test. Unlike the development pair, these work in every environment: the
service wiring (each ``build_app``) is what chooses between the two, by whether a secret is set.
"""

from collections.abc import Mapping

from factotum.common.config import ServiceSettings
from factotum.common.devauth import (
    DevAuthenticator,
    DevPrincipalDelegation,
)
from factotum.common.tokens import decode_token, encode_token
from factotum.contracts.errors import AuthError
from factotum.contracts.identity import Principal
from factotum.contracts.ids import TenantId, UserId

_AUTHORIZATION = "authorization"
_BEARER = "bearer "
DELEGATION_TTL_S = 60
"""How long a token minted to call the next service is good for: only that one call needs it."""


class HubAuthenticator:
    """Reads the ``Principal`` a bearer token proves.

    Any service holding the secret can check a token, whoever minted it: that trust, and why a
    stronger one needs asymmetric signatures instead, is in the module docstring of
    ``factotum.common.tokens``.
    """

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        lowered = {name.lower(): value for name, value in headers.items()}
        value = lowered.get(_AUTHORIZATION, "")
        if len(value) <= len(_BEARER) or value[: len(_BEARER)].lower() != _BEARER:
            raise AuthError("no bearer token was presented")
        return decode_token(value[len(_BEARER) :].strip(), secret=self._secret)


class HubPrincipalDelegation:
    """Proves a principal to the next service by minting a token of its own, fresh and short-lived:
    a service re-signs the principal it already holds rather than forwarding what it was presented
    (a device's own token, or nothing at all when the caller acts for the tenant)."""

    def __init__(self, secret: bytes, *, ttl_s: int = DELEGATION_TTL_S) -> None:
        self._secret = secret
        self._ttl_s = ttl_s

    def headers(self, principal: Principal) -> Mapping[str, str]:
        token = encode_token(principal, secret=self._secret, ttl_s=self._ttl_s)
        return {_AUTHORIZATION: f"Bearer {token}"}


def authenticator_for(
    settings: ServiceSettings, *, tenant_id: TenantId, user_id: UserId
) -> DevAuthenticator | HubAuthenticator:
    """The real authenticator once a token secret is configured; otherwise the development one,
    which does not exist outside development and test, so a service still refuses to start in
    production until a secret is set."""
    if settings.hub_secret_bytes is not None:
        return HubAuthenticator(settings.hub_secret_bytes)
    return DevAuthenticator(settings, tenant_id=tenant_id, user_id=user_id)


def principal_delegation_for(
    settings: ServiceSettings,
) -> DevPrincipalDelegation | HubPrincipalDelegation:
    """The real delegation once a token secret is configured; otherwise the development one."""
    if settings.hub_secret_bytes is not None:
        return HubPrincipalDelegation(settings.hub_secret_bytes)
    return DevPrincipalDelegation(settings)
