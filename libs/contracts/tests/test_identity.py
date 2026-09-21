import pytest
from pydantic import ValidationError

from factotum.contracts.identity import (
    Owner,
    OwnerKind,
    Principal,
    Role,
    Subject,
    SubjectKind,
    TenantMode,
)
from factotum.contracts.ids import TenantId
from factotum.contracts.testing import (
    make_device_id,
    make_tenant_id,
    make_tenant_principal,
    make_user_id,
    make_user_principal,
)


class TestSubject:
    def test_a_user_subject_carries_the_user_id(self) -> None:
        user_id = make_user_id()

        subject = Subject.user(user_id)

        assert subject.kind is SubjectKind.USER
        assert subject.id == user_id

    def test_a_tenant_subject_carries_the_tenant_id(self) -> None:
        tenant_id = make_tenant_id()

        subject = Subject.tenant(tenant_id)

        assert subject.kind is SubjectKind.TENANT
        assert subject.id == tenant_id

    def test_subjects_compare_and_hash_by_value(self) -> None:
        user_id = make_user_id()

        assert Subject.user(user_id) == Subject.user(user_id)
        assert len({Subject.user(user_id), Subject.user(user_id)}) == 1

    def test_the_same_uuid_as_user_and_as_tenant_is_not_the_same_subject(self) -> None:
        user_id = make_user_id()

        assert Subject.user(user_id) != Subject.tenant(TenantId(user_id))


class TestPrincipal:
    def test_a_user_principal_needs_at_least_one_role(self) -> None:
        with pytest.raises(ValidationError, match="at least one role"):
            make_user_principal(roles=frozenset())

    def test_a_tenant_principal_has_no_roles(self) -> None:
        tenant_id = make_tenant_id()

        with pytest.raises(ValidationError, match="no roles"):
            Principal(
                tenant_id=tenant_id,
                acts_as=Subject.tenant(tenant_id),
                roles=frozenset({Role.ADMIN}),
            )

    def test_a_tenant_subject_must_be_the_principals_own_tenant(self) -> None:
        with pytest.raises(ValidationError, match="own tenant"):
            Principal(tenant_id=make_tenant_id(), acts_as=Subject.tenant(make_tenant_id()))

    def test_personal_and_communal_principals_are_both_valid(self) -> None:
        device_id = make_device_id()

        personal = make_user_principal(device_id=device_id)
        communal = make_tenant_principal(device_id=device_id)

        assert personal.acts_as.kind is SubjectKind.USER
        assert communal.acts_as.kind is SubjectKind.TENANT
        assert communal.acts_as.id == communal.tenant_id

    def test_the_tenant_can_act_as_itself_without_a_device(self) -> None:
        tenant_id = make_tenant_id()

        principal = Principal.of_tenant(tenant_id)

        assert principal.acts_as == Subject.tenant(tenant_id)
        assert (principal.tenant_id, principal.device_id, principal.roles) == (
            tenant_id,
            None,
            frozenset(),
        )

    def test_a_principal_is_immutable(self) -> None:
        principal = make_user_principal()

        with pytest.raises(ValidationError):
            principal.device_id = make_device_id()  # type: ignore[misc]

    def test_a_principal_round_trips_through_json(self) -> None:
        principal = make_user_principal(
            roles=frozenset({Role.ADMIN, Role.MEMBER}), device_id=make_device_id()
        )

        assert Principal.model_validate_json(principal.model_dump_json()) == principal

    def test_a_payload_with_a_speaker_is_rejected(self) -> None:
        # Voice recognition is deliberately not part of the model: permissions never depend on it.
        payload = make_user_principal().model_dump(mode="json")
        payload["speaker"] = "someone"

        with pytest.raises(ValidationError, match="speaker"):
            Principal.model_validate(payload)


class TestOwner:
    def test_the_owner_of_a_user_subject_is_a_user(self) -> None:
        user_id = make_user_id()

        owner = Owner.of_subject(Subject.user(user_id))

        assert owner == Owner(kind=OwnerKind.USER, id=user_id)

    def test_the_owner_of_a_tenant_subject_is_the_tenant(self) -> None:
        tenant_id = make_tenant_id()

        owner = Owner.of_subject(Subject.tenant(tenant_id))

        assert owner == Owner(kind=OwnerKind.TENANT, id=tenant_id)

    def test_a_device_can_own_a_resource(self) -> None:
        device_id = make_device_id()

        assert Owner.of_device(device_id) == Owner(kind=OwnerKind.DEVICE, id=device_id)


def test_tenant_modes_have_stable_values() -> None:
    assert TenantMode("single_tenant") is TenantMode.SINGLE_TENANT
    assert TenantMode("multi_tenant") is TenantMode.MULTI_TENANT
