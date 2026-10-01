from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from factotum.contracts.hub import (
    BindingList,
    ChangePasswordRequest,
    CreateBindingRequest,
    CreateDeviceRequest,
    CreateExternalDeviceRequest,
    CreateModelBindingRequest,
    CreateSystemDeviceRequest,
    CreateUserRequest,
    DeviceList,
    DeviceOwner,
    LoginRequest,
    LoginResult,
    ModelBindingList,
    ModelBindingOwner,
    ModelBindingView,
    PairedDevice,
    UserList,
    UserView,
)
from factotum.contracts.identity import Owner, Role, Subject
from factotum.contracts.ids import ModelBindingId
from factotum.contracts.models import BindingOptions, ModelRole, ProviderKind
from factotum.contracts.testing import (
    make_device_id,
    make_system_device,
    make_tenant_id,
    make_user_id,
)


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


_CREATE_DEVICE: TypeAdapter[CreateSystemDeviceRequest | CreateExternalDeviceRequest] = TypeAdapter(
    CreateDeviceRequest
)


def test_a_device_request_is_routed_by_its_kind() -> None:
    system = _CREATE_DEVICE.validate_python(
        {"kind": "system", "name": "a", "hardware_id": "hw-1", "capabilities": ["audio_out"]}
    )
    external = _CREATE_DEVICE.validate_python({"kind": "external", "name": "a lamp"})

    assert isinstance(system, CreateSystemDeviceRequest)
    assert isinstance(external, CreateExternalDeviceRequest)


def test_a_device_of_ones_own_defaults_to_the_tenant() -> None:
    request = CreateExternalDeviceRequest(name="a lamp")

    assert request.owner is DeviceOwner.TENANT


def test_a_binding_needs_a_valid_skill_name() -> None:
    with pytest.raises(ValidationError):
        CreateBindingRequest(skill="Not Valid!", device_id=make_device_id())


def test_a_paired_device_round_trips_through_json() -> None:
    paired = PairedDevice(device=make_system_device(), token="a-token")

    assert PairedDevice.model_validate_json(paired.model_dump_json()) == paired


def test_a_device_list_and_a_binding_list_default_to_empty() -> None:
    assert DeviceList().system == ()
    assert DeviceList().external == ()
    assert BindingList().bindings == ()


def _model_binding_request(**changes: object) -> CreateModelBindingRequest:
    fields: dict[str, object] = {
        "role": ModelRole.CHAT_MAIN,
        "provider": ProviderKind.OLLAMA,
        "model": "llama3.1:8b",
        "base_url": "http://localhost:11434",
    }
    return CreateModelBindingRequest.model_validate({**fields, **changes})


def test_a_model_binding_of_ones_own_defaults_to_the_tenant() -> None:
    assert _model_binding_request().owner is ModelBindingOwner.TENANT


def test_a_model_binding_needs_a_secret_ref_that_looks_like_an_env_reference() -> None:
    with pytest.raises(ValidationError, match="env:VARIABLE_NAME"):
        _model_binding_request(
            provider=ProviderKind.OPENAI, secret_ref="the-key-itself", base_url=None
        )


def test_a_provider_with_no_fixed_address_needs_a_base_url() -> None:
    with pytest.raises(ValidationError, match="needs a base_url"):
        _model_binding_request(base_url=None)


def test_a_hosted_provider_needs_a_secret_ref() -> None:
    with pytest.raises(ValidationError, match="needs a secret_ref"):
        _model_binding_request(provider=ProviderKind.OPENAI, model="gpt-4o", base_url=None)


def test_a_model_binding_view_round_trips_through_json() -> None:
    view = ModelBindingView(
        id=ModelBindingId(uuid4()),
        role=ModelRole.CHAT_MAIN,
        provider=ProviderKind.OLLAMA,
        model="llama3.1:8b",
        base_url="http://localhost:11434",
        owner=Owner.of_subject(Subject.tenant(make_tenant_id())),
        secret_ref=None,
        options=BindingOptions(),
    )

    assert ModelBindingView.model_validate_json(view.model_dump_json()) == view


def test_a_model_binding_list_defaults_to_empty() -> None:
    assert ModelBindingList().bindings == ()
