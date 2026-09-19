from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.devices import (
    Binding,
    DeviceKind,
    DevicePolicy,
    DeviceRecord,
    ExternalDeviceRecord,
    SystemDeviceRecord,
)
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, Subject, SubjectKind
from factotum.contracts.ids import BindingId, SkillId
from factotum.contracts.testing import (
    make_device_id,
    make_external_device,
    make_system_device,
    make_tenant_id,
    make_user_id,
)

device_adapter: TypeAdapter[Any] = TypeAdapter(DeviceRecord)


class TestDevicePolicy:
    def test_a_personal_device_may_trigger_anything_by_default(self) -> None:
        policy = DevicePolicy.default_for(SubjectKind.USER)

        assert policy.max_effect is Effect.IRREVERSIBLE

    def test_a_communal_device_may_not_trigger_irreversible_actions_by_default(self) -> None:
        policy = DevicePolicy.default_for(SubjectKind.TENANT)

        assert policy.max_effect is Effect.REVERSIBLE

    @pytest.mark.parametrize("kind", list(SubjectKind))
    def test_by_default_no_skill_is_excluded_and_no_approval_is_asked(
        self, kind: SubjectKind
    ) -> None:
        policy = DevicePolicy.default_for(kind)

        assert policy.allowed_skills is None
        assert policy.requires_approval is False


class TestSystemDeviceRecord:
    def test_a_speaker_with_a_microphone_is_valid(self) -> None:
        device = make_system_device()

        assert device.kind is DeviceKind.SYSTEM
        assert {Capability.AUDIO_IN, Capability.AUDIO_OUT} == device.capabilities

    def test_a_screen_without_a_microphone_is_valid(self) -> None:
        device = make_system_device(
            capabilities={Capability.DISPLAY, Capability.AUDIO_OUT},
            display=DisplayInfo(width=800, height=480, touch=True),
        )

        assert Capability.AUDIO_IN not in device.capabilities
        assert device.display is not None

    def test_display_information_requires_the_display_capability(self) -> None:
        with pytest.raises(ValidationError, match="'display' capability"):
            make_system_device(display=DisplayInfo(width=800, height=480))

    @pytest.mark.parametrize("hardware_id", ["", "has space", "semi;colon", "x" * 129])
    def test_hardware_id_rejects_malformed_values(self, hardware_id: str) -> None:
        with pytest.raises(ValidationError, match="hardware_id"):
            make_system_device(hardware_id=hardware_id)

    @pytest.mark.parametrize(
        "hardware_id", ["AA:BB:CC:DD:EE:FF", "rpi5-10000000abcdef01", "a.b_c-d"]
    )
    def test_hardware_id_accepts_macs_and_serials(self, hardware_id: str) -> None:
        assert make_system_device(hardware_id=hardware_id).hardware_id == hardware_id

    def test_display_dimensions_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            DisplayInfo(width=0, height=480)


class TestExternalDeviceRecord:
    def test_an_external_device_has_no_capabilities_and_a_neutral_sensitivity(self) -> None:
        device = make_external_device()

        assert device.kind is DeviceKind.EXTERNAL
        assert device.sensitivity is Effect.READ_ONLY
        assert not hasattr(device, "capabilities")

    def test_references_to_the_outside_world_are_kept(self) -> None:
        device = ExternalDeviceRecord(
            **make_external_device().model_dump() | {"external_refs": {"entity_id": "light.k"}}
        )

        assert device.external_refs == {"entity_id": "light.k"}

    def test_a_sensitive_device_is_recorded(self) -> None:
        assert make_external_device(sensitivity=Effect.IRREVERSIBLE).sensitivity is (
            Effect.IRREVERSIBLE
        )


BUILDERS = [make_system_device, make_external_device]


@pytest.mark.parametrize("build", BUILDERS)
class TestRulesCommonToEveryDevice:
    def test_a_name_cannot_be_blank(self, build: Callable[..., Any]) -> None:
        with pytest.raises(ValidationError):
            build(name="   ")

    def test_tags_must_be_lowercase_slugs(self, build: Callable[..., Any]) -> None:
        base = build().model_dump()

        with pytest.raises(ValidationError, match="invalid tags"):
            device_adapter.validate_python(base | {"tags": {"Kitchen Light"}})

    def test_valid_tags_are_accepted(self, build: Callable[..., Any]) -> None:
        base = build().model_dump()

        device = device_adapter.validate_python(base | {"tags": {"light", "ground-floor"}})

        assert device.tags == {"light", "ground-floor"}

    def test_aliases_cannot_be_blank(self, build: Callable[..., Any]) -> None:
        base = build().model_dump()

        with pytest.raises(ValidationError, match="aliases"):
            device_adapter.validate_python(base | {"aliases": ("cucina", " ")})

    def test_a_device_cannot_be_owned_by_a_device(self, build: Callable[..., Any]) -> None:
        with pytest.raises(ValidationError, match="never by a device"):
            build(owner=Owner.of_device(make_device_id()))

    def test_a_device_can_be_owned_by_a_user_or_by_the_tenant(
        self, build: Callable[..., Any]
    ) -> None:
        tenant_id = make_tenant_id()

        by_user = build(tenant_id=tenant_id, owner=Owner.of_subject(Subject.user(make_user_id())))
        by_tenant = build(tenant_id=tenant_id, owner=Owner.of_subject(Subject.tenant(tenant_id)))

        assert by_user.owner.kind.value == "user"
        assert by_tenant.owner.kind.value == "tenant"


class TestDeviceRecordUnion:
    def test_the_kind_selects_the_model(self) -> None:
        system = make_system_device().model_dump(mode="json")
        external = make_external_device().model_dump(mode="json")

        assert isinstance(device_adapter.validate_python(system), SystemDeviceRecord)
        assert isinstance(device_adapter.validate_python(external), ExternalDeviceRecord)

    @pytest.mark.parametrize("build", BUILDERS)
    def test_a_record_round_trips_through_json(self, build: Callable[..., Any]) -> None:
        device = build()

        assert device_adapter.validate_json(device.model_dump_json()) == device

    def test_an_unknown_kind_is_rejected(self) -> None:
        payload = make_system_device().model_dump(mode="json") | {"kind": "robot"}

        with pytest.raises(ValidationError):
            device_adapter.validate_python(payload)

    def test_a_system_device_cannot_carry_external_fields(self) -> None:
        payload = make_system_device().model_dump(mode="json") | {"sensitivity": "reversible"}

        with pytest.raises(ValidationError, match="sensitivity"):
            device_adapter.validate_python(payload)


class TestBinding:
    def make(self, **overrides: Any) -> Binding:
        fields: dict[str, Any] = {
            "id": BindingId(uuid4()),
            "tenant_id": make_tenant_id(),
            "device_id": make_device_id(),
            "skill_id": SkillId(uuid4()),
        }
        return Binding(**(fields | overrides))

    def test_a_binding_is_enabled_and_empty_by_default(self) -> None:
        binding = self.make()

        assert binding.enabled is True
        assert binding.params == {}

    def test_params_hold_scalars(self) -> None:
        binding = self.make(params={"entity_id": "light.kitchen", "port": 8123, "tls": True})

        assert binding.params["port"] == 8123

    def test_params_reject_nested_values(self) -> None:
        with pytest.raises(ValidationError):
            self.make(params={"nested": {"a": 1}})

    def test_the_same_skill_on_two_devices_differs_only_in_the_params(self) -> None:
        skill_id = SkillId(uuid4())

        kitchen = self.make(skill_id=skill_id, params={"entity_id": "light.kitchen"})
        study = self.make(skill_id=skill_id, params={"entity_id": "light.study"})

        assert kitchen.skill_id == study.skill_id
        assert kitchen.params != study.params
