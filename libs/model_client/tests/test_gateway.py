"""What every call to the gateway (and the knowledge service) shares: proving an owner, in every
environment once the hub issues a secret."""

import time
from collections.abc import Mapping

import pytest

from factotum.common.config import Environment, ServiceSettings
from factotum.common.tokens import MIN_SECRET_BYTES, decode_token
from factotum.contracts.errors import AuthError
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.testing import make_device_id, make_tenant_id, make_user_id
from factotum.model_client.gateway import DevDelegation, HubDelegation, delegation_for

SECRET = b"s" * MIN_SECRET_BYTES
TENANT = make_tenant_id()


def _token_of(headers: Mapping[str, str]) -> str:
    return headers["authorization"].removeprefix("Bearer ")


class TestHubDelegation:
    def test_a_user_owner_proves_a_principal_acting_as_that_user(self) -> None:
        user_id = make_user_id()
        delegation = HubDelegation(SECRET, TENANT)

        headers = delegation.headers(Owner.of_subject(Subject.user(user_id)))

        principal = decode_token(_token_of(headers), secret=SECRET)
        assert (principal.tenant_id, principal.acts_as) == (TENANT, Subject.user(user_id))

    def test_a_tenant_owner_proves_the_principal_of_the_fixed_tenant(self) -> None:
        # Owner alone does not say which tenant it is for (a tenant's Owner.id is its tenant id,
        # but nothing here is asked to trust it): what is proved is always this delegation's own.
        delegation = HubDelegation(SECRET, TENANT)

        headers = delegation.headers(Owner.of_subject(Subject.tenant(make_tenant_id())))

        principal = decode_token(_token_of(headers), secret=SECRET)
        assert (principal.tenant_id, principal.acts_as) == (TENANT, Subject.tenant(TENANT))

    def test_a_device_owner_is_refused(self) -> None:
        delegation = HubDelegation(SECRET, TENANT)

        with pytest.raises(ValueError, match="user or by the tenant"):
            delegation.headers(Owner.of_device(make_device_id()))

    def test_the_token_is_short_lived_and_then_stops_working(self) -> None:
        delegation = HubDelegation(SECRET, TENANT, ttl_s=1)

        headers = delegation.headers(Owner.of_subject(Subject.tenant(TENANT)))

        decode_token(_token_of(headers), secret=SECRET)  # good now
        with pytest.raises(AuthError):
            decode_token(_token_of(headers), secret=SECRET, now=int(time.time()) + 2)


class TestDelegationFor:
    def test_without_a_secret_it_is_the_development_one(self) -> None:
        settings = ServiceSettings(_env_file=None, environment=Environment.TEST)

        assert isinstance(delegation_for(settings, TENANT), DevDelegation)

    def test_with_a_secret_it_is_the_hubs(self) -> None:
        settings = ServiceSettings(
            _env_file=None,
            environment=Environment.TEST,
            hub_token_secret=SECRET.decode(),  # type: ignore[arg-type]
        )

        assert isinstance(delegation_for(settings, TENANT), HubDelegation)

    def test_the_hubs_works_in_production_unlike_the_development_one(self) -> None:
        settings = ServiceSettings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            hub_token_secret=SECRET.decode(),  # type: ignore[arg-type]
        )

        delegation = delegation_for(settings, TENANT)

        assert delegation.headers(Owner.of_subject(Subject.tenant(TENANT)))
