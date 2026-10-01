import pytest

from factotum.common.credentials import verify_credential
from factotum.common.tokens import MIN_SECRET_BYTES, decode_token
from factotum.contracts.capabilities import Capability
from factotum.contracts.devices import ExternalDeviceRecord
from factotum.contracts.errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    PasswordChangeRequiredError,
    PolicyViolationError,
)
from factotum.contracts.hub import (
    ChangePasswordRequest,
    CreateBindingRequest,
    CreateExternalDeviceRequest,
    CreateModelBindingRequest,
    CreateSystemDeviceRequest,
    CreateUserRequest,
    DeviceOwner,
    LoginRequest,
    ModelBindingOwner,
    PairedDevice,
    SecretOwner,
    SetSecretRequest,
)
from factotum.contracts.identity import Owner, Principal, Role, Subject, SubjectKind
from factotum.contracts.models import ModelRole, ProviderKind
from factotum.contracts.testing import make_device_id, make_tenant_id, make_user_id
from factotum.hub.service import HubService
from factotum.hub.store.memory import InMemoryStore

SECRET = b"s" * MIN_SECRET_BYTES
USERNAME = "admin"
PASSWORD = "the-first-password"


def service() -> HubService:
    return HubService(InMemoryStore(), secret=SECRET, session_hours=1)


async def bootstrapped() -> HubService:
    svc = service()
    made = await svc.bootstrap(tenant_id=make_tenant_id(), username=USERNAME, password=PASSWORD)
    assert made
    return svc


class TestBootstrap:
    async def test_it_creates_the_tenant_only_once(self) -> None:
        svc = service()

        first = await svc.bootstrap(
            tenant_id=make_tenant_id(), username=USERNAME, password=PASSWORD
        )
        second = await svc.bootstrap(
            tenant_id=make_tenant_id(), username="somebody-else", password="irrelevant"
        )

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


async def _ready_admin(svc: HubService) -> Principal:
    """An administrator past the forced password change, who may do anything an admin can."""
    admin = await _admin_principal(svc)
    await svc.change_password(
        admin, ChangePasswordRequest(current_password=PASSWORD, new_password="a new password")
    )
    return admin


async def _member(svc: HubService, admin: Principal) -> Principal:
    await svc.create_user(admin, CreateUserRequest(username="member", password="x" * 10))
    login = await svc.login(LoginRequest(username="member", password="x" * 10))
    return decode_token(login.token, secret=SECRET)


SPEAKER = CreateSystemDeviceRequest(
    name="Kitchen speaker",
    hardware_id="kitchen-01",
    capabilities=frozenset({Capability.AUDIO_OUT}),
)
LAMP = CreateExternalDeviceRequest(name="Living room lamp")


class TestCreateDeviceAndListDevices:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        svc = await bootstrapped()
        principal = await _admin_principal(svc)

        with pytest.raises(PasswordChangeRequiredError):
            await svc.create_device(principal, SPEAKER)

    async def test_a_member_may_not_register_a_device_but_may_list_them(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        await svc.create_device(admin, LAMP)
        member = await _member(svc, admin)

        with pytest.raises(PolicyViolationError):
            await svc.create_device(member, LAMP)
        assert len((await svc.list_devices(member)).external) == 1

    async def test_a_system_device_is_paired_with_a_token_that_verifies(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)

        paired = await svc.create_device(admin, SPEAKER)

        assert isinstance(paired, PairedDevice)
        [system] = (await svc.list_devices(admin)).system
        assert verify_credential(paired.token, system.credential_hash)

    async def test_an_external_device_has_no_credential_and_is_listed(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)

        registered = await svc.create_device(admin, LAMP)

        assert isinstance(registered, ExternalDeviceRecord)
        assert (await svc.list_devices(admin)).external == (registered,)

    async def test_a_device_of_ones_own_belongs_to_the_registering_administrator(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)

        registered = await svc.create_device(
            admin, CreateExternalDeviceRequest(name="My lamp", owner=DeviceOwner.USER)
        )

        assert isinstance(registered, ExternalDeviceRecord)
        assert registered.owner == Owner.of_subject(admin.acts_as)

    async def test_a_duplicate_name_is_a_conflict(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        await svc.create_device(admin, LAMP)

        with pytest.raises(ConflictError):
            await svc.create_device(admin, LAMP)


class TestCreateBindingAndListBindings:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        svc = await bootstrapped()
        principal = await _admin_principal(svc)

        with pytest.raises(PasswordChangeRequiredError):
            await svc.create_binding(
                principal, CreateBindingRequest(skill="lamp", device_id=make_device_id())
            )

    async def test_binding_an_unknown_device_is_not_found(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)

        with pytest.raises(NotFoundError):
            await svc.create_binding(
                admin, CreateBindingRequest(skill="lamp", device_id=make_device_id())
            )

    async def test_a_binding_is_made_and_listed(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        lamp = await svc.create_device(admin, LAMP)
        assert isinstance(lamp, ExternalDeviceRecord)

        created = await svc.create_binding(
            admin,
            CreateBindingRequest(
                skill="lamp", device_id=lamp.id, params={"entity_id": "light.living_room"}
            ),
        )

        assert (created.skill, created.device_id) == ("lamp", lamp.id)
        assert (await svc.list_bindings(admin)).bindings == (created,)

    async def test_a_member_may_not_bind_but_may_list(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        lamp = await svc.create_device(admin, LAMP)
        assert isinstance(lamp, ExternalDeviceRecord)
        member = await _member(svc, admin)

        with pytest.raises(PolicyViolationError):
            await svc.create_binding(member, CreateBindingRequest(skill="lamp", device_id=lamp.id))
        assert (await svc.list_bindings(member)).bindings == ()

    async def test_the_same_skill_bound_twice_to_a_device_is_a_conflict(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        lamp = await svc.create_device(admin, LAMP)
        assert isinstance(lamp, ExternalDeviceRecord)
        await svc.create_binding(admin, CreateBindingRequest(skill="lamp", device_id=lamp.id))

        with pytest.raises(ConflictError):
            await svc.create_binding(admin, CreateBindingRequest(skill="lamp", device_id=lamp.id))


OLLAMA_CHAT = CreateModelBindingRequest(
    role=ModelRole.CHAT_MAIN,
    provider=ProviderKind.OLLAMA,
    model="llama3.1:8b",
    base_url="http://localhost:11434",
)


class TestUpsertModelBindingAndListModelBindings:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        svc = await bootstrapped()
        principal = await _admin_principal(svc)

        with pytest.raises(PasswordChangeRequiredError):
            await svc.upsert_model_binding(principal, OLLAMA_CHAT)

    async def test_a_member_may_not_bind_a_model_but_may_list(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        await svc.upsert_model_binding(admin, OLLAMA_CHAT)
        member = await _member(svc, admin)

        with pytest.raises(PolicyViolationError):
            await svc.upsert_model_binding(member, OLLAMA_CHAT)
        assert len((await svc.list_model_bindings(member)).bindings) == 1

    async def test_a_binding_is_made_and_listed(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)

        created = await svc.upsert_model_binding(admin, OLLAMA_CHAT)

        assert (created.role, created.model) == (ModelRole.CHAT_MAIN, "llama3.1:8b")
        assert (await svc.list_model_bindings(admin)).bindings == (created,)

    async def test_binding_the_same_role_again_replaces_it(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        await svc.upsert_model_binding(admin, OLLAMA_CHAT)

        replaced = await svc.upsert_model_binding(
            admin, OLLAMA_CHAT.model_copy(update={"model": "llama3.1:70b"})
        )

        listed = (await svc.list_model_bindings(admin)).bindings
        assert [b.model for b in listed] == ["llama3.1:70b"]
        assert replaced.model == "llama3.1:70b"

    async def test_a_binding_of_ones_own_belongs_to_the_registering_administrator(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)

        created = await svc.upsert_model_binding(
            admin, OLLAMA_CHAT.model_copy(update={"owner": ModelBindingOwner.USER})
        )

        assert created.owner == Owner.of_subject(admin.acts_as)


HA_TOKEN = SetSecretRequest(name="HA_TOKEN", value="a-long-lived-token")


class TestSetSecretAndListSecrets:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        svc = await bootstrapped()
        principal = await _admin_principal(svc)

        with pytest.raises(PasswordChangeRequiredError):
            await svc.set_secret(principal, HA_TOKEN)

    async def test_a_member_may_not_set_a_secret_but_may_list(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        await svc.set_secret(admin, HA_TOKEN)
        member = await _member(svc, admin)

        with pytest.raises(PolicyViolationError):
            await svc.set_secret(member, HA_TOKEN)
        assert len((await svc.list_secrets(member)).secrets) == 1

    async def test_a_secret_is_set_and_listed(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)

        created = await svc.set_secret(admin, HA_TOKEN)

        assert (created.name, created.value) == ("HA_TOKEN", "a-long-lived-token")
        assert (await svc.list_secrets(admin)).secrets == (created,)

    async def test_setting_the_same_name_again_replaces_it(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)
        await svc.set_secret(admin, HA_TOKEN)

        replaced = await svc.set_secret(admin, HA_TOKEN.model_copy(update={"value": "a-new-token"}))

        listed = (await svc.list_secrets(admin)).secrets
        assert [s.value for s in listed] == ["a-new-token"]
        assert replaced.value == "a-new-token"

    async def test_a_secret_of_ones_own_belongs_to_the_registering_administrator(self) -> None:
        svc = await bootstrapped()
        admin = await _ready_admin(svc)

        created = await svc.set_secret(
            admin, HA_TOKEN.model_copy(update={"owner": SecretOwner.USER})
        )

        assert created.owner == Owner.of_subject(admin.acts_as)
