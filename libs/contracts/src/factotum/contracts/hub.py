"""What the hub's own API says: logging in, changing a password, and the users of a tenant.

A tenant's devices, spaces, linked accounts, model bindings and skill secrets are later slices of
the hub; this is only what slice 7a needs to issue a token and let an administrator add a user.
"""

from pydantic import AwareDatetime, Field

from factotum.contracts.base import Contract
from factotum.contracts.identity import Role
from factotum.contracts.ids import UserId

_Username = Field(min_length=1, max_length=64)
_Password = Field(min_length=1, max_length=256)


class LoginRequest(Contract):
    username: str = _Username
    password: str = _Password


class LoginResult(Contract):
    token: str
    must_change_password: bool
    expires_at: AwareDatetime


class ChangePasswordRequest(Contract):
    current_password: str = _Password
    new_password: str = Field(min_length=8, max_length=256)


class CreateUserRequest(Contract):
    username: str = _Username
    password: str = _Password
    roles: frozenset[Role] = Field(default=frozenset({Role.MEMBER}), min_length=1)


class UserView(Contract):
    id: UserId
    username: str
    roles: frozenset[Role]
    must_change_password: bool
    created_at: AwareDatetime


class UserList(Contract):
    users: tuple[UserView, ...] = ()
