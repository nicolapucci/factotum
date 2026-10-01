import pytest

from factotum.common.tokens import MIN_SECRET_BYTES, decode_token
from factotum.contracts.errors import (
    AuthError,
    ConflictError,
    PasswordChangeRequiredError,
    PolicyViolationError,
)
from factotum.contracts.hub import ChangePasswordRequest, CreateUserRequest, LoginRequest
from factotum.contracts.identity import Principal, Role, Subject, SubjectKind
from factotum.contracts.testing import make_user_id
from factotum.hub.service import HubService
from factotum.hub.store.memory import InMemoryStore

SECRET = b"s" * MIN_SECRET_BYTES
USERNAME = "admin"
PASSWORD = "the-first-password"


def service() -> HubService:
    return HubService(InMemoryStore(), secret=SECRET, session_hours=1)


async def bootstrapped() -> HubService:
    svc = service()
    made = await svc.bootstrap(username=USERNAME, password=PASSWORD)
    assert made
    return svc


class TestBootstrap:
    async def test_it_creates_the_tenant_only_once(self) -> None:
        svc = service()

        first = await svc.bootstrap(username=USERNAME, password=PASSWORD)
        second = await svc.bootstrap(username="somebody-else", password="irrelevant")

        assert (first, second) == (True, False)

    async def test_the_administrator_must_change_the_password_at_first(self) -> None:
        svc = await bootstrapped()

        result = await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))

        assert result.must_change_password is True


class TestLogin:
    async def test_it_mints_a_token_that_proves_the_administrator(self) -> None:
        svc = await bootstrapped()

        result = await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))

        principal = decode_token(result.token, secret=SECRET)
        assert principal.acts_as.kind is SubjectKind.USER
        assert principal.roles == frozenset({Role.ADMIN})

    async def test_the_wrong_password_is_refused(self) -> None:
        svc = await bootstrapped()

        with pytest.raises(AuthError, match="wrong") as wrong_password:
            await svc.login(LoginRequest(username=USERNAME, password="not it"))
        with pytest.raises(AuthError, match="wrong") as wrong_username:
            await svc.login(LoginRequest(username="nobody", password=PASSWORD))

        # The same message either way: which part was wrong is not said.
        assert str(wrong_password.value) == str(wrong_username.value)


class TestMe:
    async def test_it_is_the_caller_of_the_token(self) -> None:
        svc = await bootstrapped()
        result = await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))
        principal = decode_token(result.token, secret=SECRET)

        me = await svc.me(principal)

        assert (me.username, me.must_change_password) == (USERNAME, True)

    async def test_a_tenant_acting_for_itself_has_no_user_to_be(self) -> None:
        svc = await bootstrapped()
        result = await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))
        principal = decode_token(result.token, secret=SECRET)

        with pytest.raises(AuthError, match="not a user"):
            await svc.me(Principal.of_tenant(principal.tenant_id))

    async def test_a_token_of_a_user_who_is_no_longer_there_is_refused(self) -> None:
        svc = await bootstrapped()
        result = await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))
        gone = decode_token(result.token, secret=SECRET).model_copy(
            update={"acts_as": Subject.user(make_user_id())}
        )

        with pytest.raises(AuthError, match="no longer exists"):
            await svc.me(gone)


class TestChangePassword:
    async def test_it_requires_the_current_password(self) -> None:
        svc = await bootstrapped()
        result = await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))
        principal = decode_token(result.token, secret=SECRET)

        with pytest.raises(AuthError, match="wrong"):
            await svc.change_password(
                principal, ChangePasswordRequest(current_password="wrong", new_password="x" * 10)
            )

    async def test_it_clears_the_flag_and_the_old_password_stops_working(self) -> None:
        svc = await bootstrapped()
        result = await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))
        principal = decode_token(result.token, secret=SECRET)

        await svc.change_password(
            principal,
            ChangePasswordRequest(current_password=PASSWORD, new_password="a new password"),
        )

        again = await svc.login(LoginRequest(username=USERNAME, password="a new password"))
        assert again.must_change_password is False
        with pytest.raises(AuthError):
            await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))


async def _admin_principal(svc: HubService) -> Principal:
    result = await svc.login(LoginRequest(username=USERNAME, password=PASSWORD))
    return decode_token(result.token, secret=SECRET)


class TestCreateUserAndListUsers:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        svc = await bootstrapped()
        principal = await _admin_principal(svc)

        with pytest.raises(PasswordChangeRequiredError):
            await svc.create_user(
                principal, CreateUserRequest(username="member", password="a password")
            )
        with pytest.raises(PasswordChangeRequiredError):
            await svc.list_users(principal)

    async def test_a_member_may_not_add_or_list_users(self) -> None:
        svc = await bootstrapped()
        admin = await _admin_principal(svc)
        await svc.change_password(
            admin, ChangePasswordRequest(current_password=PASSWORD, new_password="new password")
        )
        created = await svc.create_user(
            admin, CreateUserRequest(username="member", password="member password")
        )
        member_login = await svc.login(LoginRequest(username="member", password="member password"))
        member_principal = decode_token(member_login.token, secret=SECRET)
        assert created.roles == frozenset({Role.MEMBER})

        with pytest.raises(PolicyViolationError):
            await svc.create_user(
                member_principal, CreateUserRequest(username="another", password="a password")
            )
        with pytest.raises(PolicyViolationError):
            await svc.list_users(member_principal)

    async def test_an_administrator_adds_a_user_and_lists_both(self) -> None:
        svc = await bootstrapped()
        admin = await _admin_principal(svc)
        await svc.change_password(
            admin, ChangePasswordRequest(current_password=PASSWORD, new_password="new password")
        )

        await svc.create_user(admin, CreateUserRequest(username="member", password="x" * 10))
        listed = await svc.list_users(admin)

        assert sorted(user.username for user in listed.users) == ["admin", "member"]

    async def test_a_duplicate_username_is_a_conflict(self) -> None:
        svc = await bootstrapped()
        admin = await _admin_principal(svc)
        await svc.change_password(
            admin, ChangePasswordRequest(current_password=PASSWORD, new_password="new password")
        )

        with pytest.raises(ConflictError):
            await svc.create_user(admin, CreateUserRequest(username=USERNAME, password="x" * 10))
