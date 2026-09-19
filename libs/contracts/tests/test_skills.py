from typing import Any

import pytest
from pydantic import ValidationError

from factotum.contracts.effects import Effect
from factotum.contracts.skills import (
    AccountRequirement,
    AccountScope,
    ApprovalPolicy,
    ConfirmPolicy,
    ParameterSpec,
    ParamType,
    SkillManifest,
    SkillMode,
    SkillPermissions,
    SkillTarget,
)

LIGHTS: dict[str, Any] = {
    "name": "living-room-lights",
    "description": "Turn the living room lights on or off, or set their brightness.",
    "examples": ["turn on the living room lights", "dim the lounge light to 30%"],
    "target": "external",
    "parameters": {
        "state": {"type": "string", "enum": ["on", "off"]},
        "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["state"],
    "device_parameters": {"entity_id": {"type": "string"}, "host": {"type": "string"}},
    "permissions": {
        "network": ["${device.host}"],
        "secrets": ["HA_TOKEN"],
        "accounts": [{"provider": "home-assistant", "scope": "tenant"}],
    },
    "effect": "reversible",
    "confirm": "never",
    "entrypoint": "scripts/run.py",
    "timeout_s": 10,
}


def manifest(**overrides: Any) -> SkillManifest:
    return SkillManifest.model_validate(LIGHTS | overrides)


def test_the_documented_example_is_a_valid_manifest() -> None:
    skill = manifest()

    assert skill.target is SkillTarget.EXTERNAL
    assert skill.effect is Effect.REVERSIBLE
    assert skill.mode is SkillMode.SCRIPT
    assert skill.permissions.accounts == (
        AccountRequirement(provider="home-assistant", scope=AccountScope.TENANT),
    )


def test_a_minimal_skill_only_needs_a_name_a_description_and_an_effect() -> None:
    skill = SkillManifest.model_validate(
        {"name": "weather", "description": "Tell the weather.", "effect": "read_only"}
    )

    assert skill.target is SkillTarget.NONE
    assert skill.confirm is ConfirmPolicy.NEVER
    assert skill.approval is ApprovalPolicy.NONE
    assert skill.timeout_s == 10


def test_the_author_must_declare_the_effect() -> None:
    payload = {"name": "weather", "description": "Tell the weather."}

    with pytest.raises(ValidationError, match="effect"):
        SkillManifest.model_validate(payload)


def test_without_an_entrypoint_the_skill_runs_in_instructions_mode() -> None:
    assert manifest(entrypoint=None).mode is SkillMode.INSTRUCTIONS


@pytest.mark.parametrize("name", ["", "A", "Living Room", "-lights", "x", "a" * 64])
def test_the_name_must_be_a_slug(name: str) -> None:
    with pytest.raises(ValidationError, match="name"):
        manifest(name=name)


@pytest.mark.parametrize("seconds", [0, 121])
def test_the_timeout_is_bounded(seconds: int) -> None:
    with pytest.raises(ValidationError, match="timeout_s"):
        manifest(timeout_s=seconds)


def test_blank_examples_are_rejected() -> None:
    with pytest.raises(ValidationError, match="examples"):
        manifest(examples=["turn on the light", "  "])


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="run_as_root"):
        manifest(run_as_root=True)


class TestEntrypoint:
    @pytest.mark.parametrize(
        "path",
        ["/etc/passwd", "../outside.py", "scripts/../../x.py", "C:\\x.py", "scripts//run.py", ""],
    )
    def test_a_path_that_leaves_the_skill_folder_is_rejected(self, path: str) -> None:
        with pytest.raises(ValidationError, match="entrypoint"):
            manifest(entrypoint=path)

    @pytest.mark.parametrize("path", ["run.py", "scripts/run.py", "a/b/c.py"])
    def test_a_relative_path_is_accepted(self, path: str) -> None:
        assert manifest(entrypoint=path).entrypoint == path


class TestParameters:
    def test_required_names_must_be_declared(self) -> None:
        with pytest.raises(ValidationError, match="not declared in parameters"):
            manifest(required=["state", "colour"])

    def test_a_name_cannot_be_both_a_call_and_a_device_parameter(self) -> None:
        with pytest.raises(ValidationError, match="both as parameters and device_parameters"):
            manifest(device_parameters={"state": {"type": "string"}, "host": {"type": "string"}})

    def test_device_parameters_need_an_external_target(self) -> None:
        with pytest.raises(ValidationError, match="only allowed when the target is 'external'"):
            manifest(target="none", permissions={})

    @pytest.mark.parametrize("name", ["State", "1st", "has-dash", "x" * 65])
    def test_parameter_names_are_snake_case_identifiers(self, name: str) -> None:
        with pytest.raises(ValidationError, match="invalid parameter names"):
            manifest(parameters={name: {"type": "string"}}, required=[])

    def test_the_json_schema_describes_the_call_parameters(self) -> None:
        schema = manifest().to_json_schema()

        assert schema == {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["on", "off"]},
                "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["state"],
            "additionalProperties": False,
        }

    def test_the_device_schema_requires_every_device_parameter(self) -> None:
        schema = manifest().device_parameters_json_schema()

        assert schema["required"] == ["entity_id", "host"]
        assert schema["additionalProperties"] is False


class TestParameterSpec:
    def test_yaml_booleans_are_caught_when_a_string_enum_was_meant(self) -> None:
        # In YAML 1.1 an unquoted `on` / `off` is parsed as True / False.
        with pytest.raises(ValidationError, match="do not match type string"):
            ParameterSpec(type=ParamType.STRING, enum=(True, False))

    @pytest.mark.parametrize(
        ("param_type", "values"),
        [
            (ParamType.STRING, ("a", "b")),
            (ParamType.INTEGER, (1, 2)),
            (ParamType.NUMBER, (1, 2.5)),
            (ParamType.BOOLEAN, (True,)),
        ],
    )
    def test_enum_values_of_the_declared_type_are_accepted(
        self, param_type: ParamType, values: tuple[Any, ...]
    ) -> None:
        assert ParameterSpec(type=param_type, enum=values).enum == values

    def test_a_boolean_is_not_an_integer(self) -> None:
        with pytest.raises(ValidationError, match="do not match type integer"):
            ParameterSpec(type=ParamType.INTEGER, enum=(True,))

    def test_bounds_apply_to_numbers_only(self) -> None:
        with pytest.raises(ValidationError, match="numeric"):
            ParameterSpec(type=ParamType.STRING, minimum=1)

    def test_the_minimum_cannot_exceed_the_maximum(self) -> None:
        with pytest.raises(ValidationError, match="cannot exceed"):
            ParameterSpec(type=ParamType.INTEGER, minimum=10, maximum=1)

    def test_an_enum_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            ParameterSpec(type=ParamType.STRING, enum=())


class TestPermissions:
    @pytest.mark.parametrize(
        "entry", ["api.example.org", "*.example.org", "192.168.1.50", "homeassistant.local"]
    )
    def test_hosts_are_accepted(self, entry: str) -> None:
        assert SkillPermissions(network=(entry,)).network == (entry,)

    @pytest.mark.parametrize(
        "entry", ["https://api.example.org", "api.example.org/path", "bad host", "${x}", "-a.com"]
    )
    def test_anything_that_is_not_a_host_or_a_device_template_is_rejected(self, entry: str) -> None:
        with pytest.raises(ValidationError, match="invalid network entries"):
            SkillPermissions(network=(entry,))

    @pytest.mark.parametrize("name", ["ha_token", "1TOKEN", "HA-TOKEN", ""])
    def test_secret_names_are_upper_snake_case(self, name: str) -> None:
        with pytest.raises(ValidationError, match="invalid secret names"):
            SkillPermissions(secrets=(name,))

    def test_device_templates_are_collected(self) -> None:
        permissions = SkillPermissions(network=("${device.host}", "api.example.org"))

        assert permissions.device_templates() == {"host"}

    def test_a_template_must_refer_to_a_declared_device_parameter(self) -> None:
        with pytest.raises(ValidationError, match="undeclared device parameters"):
            manifest(permissions={"network": ["${device.ip}"]})

    def test_nothing_is_granted_by_default(self) -> None:
        permissions = SkillPermissions()

        assert permissions.network == ()
        assert permissions.secrets == ()
        assert permissions.accounts == ()
