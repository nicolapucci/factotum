import pytest

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevAuthenticator, DevPrincipalDelegation
from factotum.common.hubauth import (
    DELEGATION_TTL_S,
    HubAuthenticator,
    HubPrincipalDelegation,
    authenticator_for,
    principal_delegation_for,
)
from factotum.common.tokens import decode_token, encode_token
from factotum.contracts.errors import AuthError
from factotum.contracts.testing import make_tenant_id, make_user_id, make_user_principal

SECRET = b"s" * 16


def _settings(environment: Environment, secret: str | None = None) -> ServiceSettings:
    return ServiceSettings(
        _env_file=None,
        environment=environment,
        hub_token_secret=secret,  # type: ignore[arg-type]
    )


class TestHubAuthenticator:
    def test_a_bearer_token_is_read_back_to_the_same_principal(self) -> None:
        principal = make_user_principal()
        token = encode_token(principal, secret=SECRET, ttl_s=60)

        read = HubAuthenticator(SECRET).authenticate({"Authorization": f"Bearer {token}"})

        assert read == principal

    def test_the_header_name_and_the_scheme_are_case_insensitive(self) -> None:
        principal = make_user_principal()
        token = encode_token(principal, secret=SECRET, ttl_s=60)

        read = HubAuthenticator(SECRET).authenticate({"AUTHORIZATION": f"bearer {token}"})

        assert read == principal

    def test_no_header_is_refused(self) -> None:
        with pytest.raises(AuthError, match="no bearer token"):
            HubAuthenticator(SECRET).authenticate({})

    def test_a_header_that_is_not_a_bearer_token_is_refused(self) -> None:
        with pytest.raises(AuthError, match="no bearer token"):
            HubAuthenticator(SECRET).authenticate({"authorization": "Basic dXNlcjpwYXNz"})

    def test_an_empty_bearer_token_is_refused(self) -> None:
        with pytest.raises(AuthError):
            HubAuthenticator(SECRET).authenticate({"authorization": "Bearer "})


class TestHubPrincipalDelegation:
    def test_it_mints_a_token_that_proves_the_principal(self) -> None:
        principal = make_user_principal()

        headers = HubPrincipalDelegation(SECRET).headers(principal)
        token = headers["authorization"].removeprefix("Bearer ")

        assert decode_token(token, secret=SECRET) == principal

    def test_the_minted_token_is_short_lived(self) -> None:
        assert 0 < DELEGATION_TTL_S <= 300


class TestChoosingBetweenTheRealAndTheDevelopmentOnes:
    def test_a_secret_selects_the_real_authenticator(self) -> None:
        authenticator = authenticator_for(
            _settings(Environment.PRODUCTION, secret=SECRET.decode("ascii")),
            tenant_id=make_tenant_id(),
            user_id=make_user_id(),
        )

        assert isinstance(authenticator, HubAuthenticator)

    def test_no_secret_falls_back_to_development_in_development(self) -> None:
        authenticator = authenticator_for(
            _settings(Environment.DEVELOPMENT), tenant_id=make_tenant_id(), user_id=make_user_id()
        )

        assert isinstance(authenticator, DevAuthenticator)

    def test_no_secret_still_refuses_to_start_in_production(self) -> None:
        with pytest.raises(AuthError, match="disabled outside"):
            authenticator_for(
                _settings(Environment.PRODUCTION),
                tenant_id=make_tenant_id(),
                user_id=make_user_id(),
            )

    def test_the_delegation_follows_the_same_rule(self) -> None:
        with_secret = principal_delegation_for(
            _settings(Environment.TEST, secret=SECRET.decode("ascii"))
        )
        without = principal_delegation_for(_settings(Environment.TEST))

        assert isinstance(with_secret, HubPrincipalDelegation)
        assert isinstance(without, DevPrincipalDelegation)
