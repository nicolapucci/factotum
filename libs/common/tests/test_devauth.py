from uuid import uuid4

import pytest

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import (
    DEV_TENANT_ID,
    DEV_USER_ID,
    DEVICE_HEADER,
    SUBJECT_HEADER,
    DevAuthenticator,
    DevPrincipalDelegation,
    dev_headers,
)
from factotum.contracts.errors import AuthError
from factotum.contracts.identity import Role, SubjectKind
from factotum.contracts.ids import DeviceId
from factotum.contracts.testing import (
    make_tenant_id,
    make_tenant_principal,
    make_user_id,
    make_user_principal,
)


def settings(environment: Environment) -> ServiceSettings:
    return ServiceSettings(_env_file=None, environment=environment)


def authenticator(environment: Environment = Environment.DEVELOPMENT) -> DevAuthenticator:
    return DevAuthenticator(
        settings(environment), tenant_id=make_tenant_id(), user_id=make_user_id()
    )


@pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
def test_it_is_available_in_development_and_test(environment: Environment) -> None:
    assert authenticator(environment).authenticate({}) is not None


def test_it_refuses_to_exist_in_production() -> None:
    with pytest.raises(AuthError, match="disabled outside"):
        authenticator(Environment.PRODUCTION)


def test_an_unconfigured_environment_leaves_it_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTOTUM_ENVIRONMENT", raising=False)

    with pytest.raises(AuthError):
        DevAuthenticator(
            ServiceSettings(_env_file=None), tenant_id=make_tenant_id(), user_id=make_user_id()
        )


def test_by_default_the_request_acts_as_the_development_user() -> None:
    tenant_id, user_id = make_tenant_id(), make_user_id()
    dev = DevAuthenticator(settings(Environment.TEST), tenant_id=tenant_id, user_id=user_id)

    principal = dev.authenticate({})

    assert principal.tenant_id == tenant_id
    assert principal.acts_as.kind is SubjectKind.USER
    assert principal.acts_as.id == user_id
    assert principal.roles == {Role.ADMIN}
    assert principal.device_id is None


def test_a_header_makes_the_request_act_as_the_tenant_like_a_communal_device() -> None:
    principal = authenticator().authenticate({SUBJECT_HEADER: "tenant"})

    assert principal.acts_as.kind is SubjectKind.TENANT
    assert principal.acts_as.id == principal.tenant_id
    assert principal.roles == frozenset()


def test_the_device_header_sets_the_device() -> None:
    device_id = uuid4()

    principal = authenticator().authenticate({DEVICE_HEADER: str(device_id)})

    assert principal.device_id == device_id


def test_header_names_are_case_insensitive() -> None:
    principal = authenticator().authenticate({"X-Factotum-Dev-Subject": " Tenant "})

    assert principal.acts_as.kind is SubjectKind.TENANT


def test_an_invalid_device_id_is_refused() -> None:
    with pytest.raises(AuthError, match="not a valid id"):
        authenticator().authenticate({DEVICE_HEADER: "not-a-uuid"})


def test_an_unknown_subject_is_refused() -> None:
    with pytest.raises(AuthError, match="unknown development subject"):
        authenticator().authenticate({SUBJECT_HEADER: "root"})


def test_the_shared_development_identities_are_distinct_valid_ids() -> None:
    assert DEV_TENANT_ID != DEV_USER_ID
    assert DEV_TENANT_ID.version == 4
    assert DEV_USER_ID.version == 4


def test_the_shared_development_identities_work_with_the_authenticator() -> None:
    dev = DevAuthenticator(settings(Environment.TEST), tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)

    principal = dev.authenticate({})

    assert principal.tenant_id == DEV_TENANT_ID
    assert principal.acts_as.id == DEV_USER_ID


class TestActingForAPrincipalInDevelopment:
    @pytest.mark.parametrize("acts_for", ["a person", "the tenant"])
    @pytest.mark.parametrize("on_a_device", [True, False])
    def test_what_a_service_sends_is_read_back_as_the_same_principal(
        self, acts_for: str, on_a_device: bool
    ) -> None:
        tenant_id, user_id = make_tenant_id(), make_user_id()
        device = DeviceId(uuid4()) if on_a_device else None
        principal = (
            make_user_principal(tenant_id=tenant_id, user_id=user_id, device_id=device)
            if acts_for == "a person"
            else make_tenant_principal(tenant_id=tenant_id, device_id=device)
        )
        dev = DevAuthenticator(settings(Environment.TEST), tenant_id=tenant_id, user_id=user_id)

        read = dev.authenticate(dev_headers(principal))

        assert (read.tenant_id, read.device_id, read.acts_as) == (
            principal.tenant_id,
            principal.device_id,
            principal.acts_as,
        )

    def test_a_device_is_named_only_when_there_is_one(self) -> None:
        tenant = make_tenant_principal(tenant_id=make_tenant_id())

        assert dev_headers(tenant) == {SUBJECT_HEADER: "tenant"}


class TestTheDelegationOfAServiceThatCallsAnother:
    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_says_whom_the_caller_acts_for_as_the_headers_the_other_side_reads(
        self, environment: Environment
    ) -> None:
        principal = make_user_principal(
            tenant_id=make_tenant_id(), user_id=make_user_id(), device_id=DeviceId(uuid4())
        )

        headers = DevPrincipalDelegation(settings(environment)).headers(principal)

        assert dict(headers) == dev_headers(principal)

    def test_it_refuses_to_exist_in_production(self) -> None:
        with pytest.raises(AuthError, match="disabled outside"):
            DevPrincipalDelegation(settings(Environment.PRODUCTION))

    def test_an_unconfigured_environment_leaves_it_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FACTOTUM_ENVIRONMENT", raising=False)

        with pytest.raises(AuthError, match="disabled outside"):
            DevPrincipalDelegation(ServiceSettings(_env_file=None))
