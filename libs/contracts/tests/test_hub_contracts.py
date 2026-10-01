from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from factotum.contracts.hub import (
    ChangePasswordRequest,
    CreateUserRequest,
    LoginRequest,
    LoginResult,
    UserList,
    UserView,
)
from factotum.contracts.identity import Role
from factotum.contracts.testing import make_user_id


def test_a_created_user_defaults_to_a_member_with_no_roles_named() -> None:
    assert CreateUserRequest(username="bob", password="a password").roles == frozenset(
        {Role.MEMBER}
    )


def test_a_created_user_needs_at_least_one_role() -> None:
    with pytest.raises(ValidationError):
        CreateUserRequest(username="bob", password="a password", roles=frozenset())


def test_a_new_password_has_a_minimum_length() -> None:
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password="whatever", new_password="short")


def test_a_login_request_round_trips_through_json() -> None:
    request = LoginRequest(username="admin", password="hunter2")

    assert LoginRequest.model_validate_json(request.model_dump_json()) == request


def test_a_user_view_round_trips_through_json() -> None:
    view = UserView(
        id=make_user_id(),
        username="admin",
        roles=frozenset({Role.ADMIN}),
        must_change_password=True,
        created_at=datetime.now(UTC),
    )

    assert UserView.model_validate_json(view.model_dump_json()) == view


def test_a_user_list_defaults_to_empty() -> None:
    assert UserList().users == ()


def test_the_contracts_are_immutable_and_reject_unknown_fields() -> None:
    result = LoginResult(token="t", must_change_password=False, expires_at=datetime.now(UTC))

    with pytest.raises(ValidationError):
        result.token = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        LoginRequest.model_validate({"username": "a", "password": "b", "extra": 1})
