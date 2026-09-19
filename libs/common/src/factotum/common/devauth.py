"""Authentication for development and tests only.

It lets services run and be exercised before the hub, and the real tokens, exist. It refuses to be
constructed unless the environment is explicitly ``development`` or ``test``; since an unset
environment means production, forgetting to configure one leaves it disabled.
"""

from collections.abc import Mapping
from uuid import UUID

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.errors import AuthError
from factotum.contracts.identity import Principal, Role, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId

DEV_TENANT_ID = TenantId(UUID("00000000-0000-4000-8000-000000000001"))
DEV_USER_ID = UserId(UUID("00000000-0000-4000-8000-000000000002"))
"""The tenant and the user every service uses in development: the same headers mean the same
people everywhere, and a bindings file or a fixture written for one service works for the others."""

SUBJECT_HEADER = "x-factotum-dev-subject"
"""``user`` (the default) or ``tenant``: whom the simulated device acts for."""

DEVICE_HEADER = "x-factotum-dev-device"
"""Optional id of the simulated device."""


class DevAuthenticator:
    """Builds a ``Principal`` for a fixed development tenant and user from request headers."""

    def __init__(
        self,
        settings: ServiceSettings,
        *,
        tenant_id: TenantId,
        user_id: UserId,
        roles: frozenset[Role] = frozenset({Role.ADMIN}),
    ) -> None:
        if settings.environment not in {Environment.DEVELOPMENT, Environment.TEST}:
            raise AuthError(
                "the development authenticator is disabled outside the development and test "
                f"environments (environment is {settings.environment.value!r})"
            )
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._roles = roles

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        lowered = {name.lower(): value for name, value in headers.items()}
        device_id = self._device_id(lowered.get(DEVICE_HEADER))
        subject = lowered.get(SUBJECT_HEADER, "user").strip().lower()

        if subject == "user":
            return Principal(
                tenant_id=self._tenant_id,
                device_id=device_id,
                acts_as=Subject.user(self._user_id),
                roles=self._roles,
            )
        if subject == "tenant":
            return Principal(
                tenant_id=self._tenant_id,
                device_id=device_id,
                acts_as=Subject.tenant(self._tenant_id),
            )
        raise AuthError(f"unknown development subject {subject!r}: use 'user' or 'tenant'")

    @staticmethod
    def _device_id(raw: str | None) -> DeviceId | None:
        if raw is None:
            return None
        try:
            return DeviceId(UUID(raw.strip()))
        except ValueError:
            raise AuthError("the development device header is not a valid id") from None
