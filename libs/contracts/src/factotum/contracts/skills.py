"""The skill manifest: how a user teaches Factotum to do something.

A skill is a folder with a ``SKILL.md`` (YAML frontmatter plus markdown instructions) and,
optionally, scripts. This module defines the *manifest*: the validated shape of the frontmatter.
Reading the file and running the skill belong to the ``actions`` service.

A skill declares everything it needs (network hosts, secrets, accounts) and how risky it is
(``effect``, ``confirm``, ``approval``). Nothing it did not declare is available to it.
"""

import re
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from factotum.contracts.base import Contract
from factotum.contracts.effects import Effect

ParamValue = str | int | float | bool

_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
HOST_PATTERN = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$",
    re.IGNORECASE,
)
_DEVICE_TEMPLATE = re.compile(r"^\$\{device\.([a-z][a-z0-9_]{0,63})\}$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SKILL_PATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")

RESERVED_ENV_PREFIXES = ("PYTHON", "LD_", "FACTOTUM_")
RESERVED_ENV_NAMES = frozenset(
    {"PATH", "HOME", "USER", "HOSTNAME", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    | {"ALL_PROXY", "SHELL", "PWD", "LANG"}
)
"""What the runner sets for a job itself, and what would change how a program is started: a secret
cannot be called any of these."""


def is_reserved_env_name(name: str) -> bool:
    return name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_PREFIXES)


def is_skill_path(path: str) -> bool:
    """Whether ``path`` says where a file is inside the folder of a skill: names of letters, digits,
    dots, dashes and underscores, separated by ``/``, that never go up. Whatever is not one cannot
    be carried to a container, so a folder with such a file is not a skill."""
    return bool(_SKILL_PATH.fullmatch(path)) and not any(
        part in {".", ".."} for part in path.split("/")
    )


def device_template_name(entry: str) -> str | None:
    """The device parameter that a network entry refers to (``${device.host}`` is ``host``), or
    nothing when it is a host."""
    match = _DEVICE_TEMPLATE.fullmatch(entry)
    return match.group(1) if match else None


class SkillTarget(StrEnum):
    """What the skill acts on."""

    NONE = "none"
    """No device: answering from a service, for instance the weather."""

    SYSTEM = "system"
    """A system device, through the ``SystemDevice`` interface."""

    EXTERNAL = "external"
    """An external device, through the bindings that attach the skill to it."""


class SkillMode(StrEnum):
    SCRIPT = "script"
    """Runs the entrypoint in the sandboxed runner."""

    INSTRUCTIONS = "instructions"
    """The markdown is followed by the model using only the platform's primitives."""


class ConfirmPolicy(StrEnum):
    """When the assistant asks a plain yes or no before acting (it authenticates nobody)."""

    NEVER = "never"
    ASK = "ask"
    """Ask whenever the action has an effect."""

    ALWAYS = "always"
    """Ask even for read-only actions."""


class ApprovalPolicy(StrEnum):
    """Whether an out-of-band approval by an authorized person is required."""

    NONE = "none"
    REQUIRED = "required"


class AccountScope(StrEnum):
    """Whose linked account a skill uses."""

    DEVICE = "device"
    """The target device's own account (the TV's streaming account)."""

    USER = "user"
    """The acting user's account. Not available on a communal device."""

    TENANT = "tenant"


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


def _matches(param_type: ParamType, value: ParamValue) -> bool:
    match param_type:
        case ParamType.STRING:
            return isinstance(value, str)
        case ParamType.BOOLEAN:
            return isinstance(value, bool)
        case ParamType.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        case ParamType.NUMBER:
            return isinstance(value, int | float) and not isinstance(value, bool)


class ParameterSpec(Contract):
    """One parameter of a skill: a primitive type with optional bounds or a closed set of values.

    Note for authors: in YAML 1.1 ``on`` and ``off`` are booleans. Quote them (``"on"``) when they
    are meant as strings; the check on ``enum`` values below turns that mistake into a clear error.
    """

    type: ParamType
    description: str | None = Field(default=None, max_length=300)
    enum: tuple[ParamValue, ...] | None = Field(default=None, min_length=1, max_length=50)
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _is_consistent(self) -> Self:
        numeric = self.type in {ParamType.INTEGER, ParamType.NUMBER}
        if (self.minimum is not None or self.maximum is not None) and not numeric:
            raise ValueError("minimum and maximum apply to numeric parameters only")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot exceed maximum")
        if self.enum is not None:
            mismatched = [value for value in self.enum if not _matches(self.type, value)]
            if mismatched:
                raise ValueError(f"enum values {mismatched} do not match type {self.type.value}")
        return self

    def to_json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type.value}
        if self.description is not None:
            schema["description"] = self.description
        if self.enum is not None:
            schema["enum"] = list(self.enum)
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        return schema


class AccountRequirement(Contract):
    """A linked account the skill needs, such as ``spotify`` at user scope."""

    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    scope: AccountScope


class SkillPermissions(Contract):
    """Everything a skill asks for. The user reviews and approves it at installation.

    A network entry is a host name (``api.example.org``, ``*.example.org``) or a reference to one
    of the skill's device parameters (``${device.host}``), resolved when the skill is bound to a
    device. Secrets are referenced by name and never shown to the model.
    """

    network: tuple[str, ...] = Field(default=(), max_length=20)
    secrets: tuple[str, ...] = Field(default=(), max_length=20)
    accounts: tuple[AccountRequirement, ...] = Field(default=(), max_length=10)

    @field_validator("network")
    @classmethod
    def _network_entries_are_hosts_or_device_templates(
        cls, entries: tuple[str, ...]
    ) -> tuple[str, ...]:
        invalid = [
            entry
            for entry in entries
            if not (HOST_PATTERN.fullmatch(entry) or _DEVICE_TEMPLATE.fullmatch(entry))
        ]
        if invalid:
            raise ValueError(f"invalid network entries {invalid}: use a host name or ${{device.x}}")
        return entries

    @field_validator("secrets")
    @classmethod
    def _secrets_are_upper_snake_case(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        invalid = [name for name in names if not _SECRET_NAME.fullmatch(name)]
        if invalid:
            raise ValueError(f"invalid secret names {invalid}: use UPPER_SNAKE_CASE")
        reserved = [name for name in names if is_reserved_env_name(name)]
        if reserved:
            raise ValueError(
                f"secret names {reserved} are ones that the runner sets itself or that change how "
                "a program starts: use another"
            )
        return names

    def device_templates(self) -> frozenset[str]:
        """Names of the device parameters referenced by the network entries."""
        names = (device_template_name(entry) for entry in self.network)
        return frozenset(name for name in names if name is not None)


class SkillManifest(Contract):
    """The frontmatter of a ``SKILL.md``."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    description: str = Field(min_length=1, max_length=500)
    examples: tuple[str, ...] = Field(default=(), max_length=20)
    target: SkillTarget = SkillTarget.NONE

    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    """Values that change at every call, extracted from what the user said."""

    required: tuple[str, ...] = ()

    device_parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    """Values fixed for each device the skill is bound to (an entity id, a topic...)."""

    permissions: SkillPermissions = Field(default_factory=SkillPermissions)

    effect: Effect
    """How much the skill changes the world. Required: the author must decide."""

    confirm: ConfirmPolicy = ConfirmPolicy.NEVER
    approval: ApprovalPolicy = ApprovalPolicy.NONE
    entrypoint: str | None = None
    timeout_s: int = Field(default=10, ge=1, le=120)

    @field_validator("examples")
    @classmethod
    def _examples_are_not_blank(cls, examples: tuple[str, ...]) -> tuple[str, ...]:
        if any(not example.strip() for example in examples):
            raise ValueError("examples cannot be blank")
        return examples

    @field_validator("parameters", "device_parameters")
    @classmethod
    def _parameter_names_are_identifiers(
        cls, specs: dict[str, ParameterSpec]
    ) -> dict[str, ParameterSpec]:
        invalid = [name for name in specs if not _PARAM_NAME.fullmatch(name)]
        if invalid:
            raise ValueError(f"invalid parameter names {invalid}: use lowercase snake_case")
        return specs

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint_stays_inside_the_skill(cls, entrypoint: str | None) -> str | None:
        if entrypoint is None:
            return None
        if not is_skill_path(entrypoint):
            raise ValueError(
                "entrypoint must be a relative path inside the skill folder, of names made of "
                "letters, digits, dots, dashes and underscores"
            )
        if not entrypoint.endswith(".py"):
            raise ValueError("only Python scripts are run: entrypoint is a .py file")
        return entrypoint

    @model_validator(mode="after")
    def _is_consistent(self) -> Self:
        unknown = sorted(set(self.required) - set(self.parameters))
        if unknown:
            raise ValueError(f"required names {unknown} are not declared in parameters")
        shared = sorted(set(self.parameters) & set(self.device_parameters))
        if shared:
            raise ValueError(
                f"names {shared} are declared both as parameters and device_parameters"
            )
        if self.device_parameters and self.target is not SkillTarget.EXTERNAL:
            raise ValueError("device_parameters are only allowed when the target is 'external'")
        missing = sorted(self.permissions.device_templates() - set(self.device_parameters))
        if missing:
            raise ValueError(f"network refers to undeclared device parameters {missing}")
        return self

    @property
    def mode(self) -> SkillMode:
        return SkillMode.SCRIPT if self.entrypoint else SkillMode.INSTRUCTIONS

    def to_json_schema(self) -> dict[str, Any]:
        """JSON Schema of the call parameters, used to extract and validate them."""
        return _object_schema(self.parameters, self.required)

    def device_parameters_json_schema(self) -> dict[str, Any]:
        """JSON Schema of the per-device parameters, used to validate a binding."""
        return _object_schema(self.device_parameters, tuple(self.device_parameters))


def _object_schema(specs: dict[str, ParameterSpec], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: spec.to_json_schema() for name, spec in specs.items()},
        "required": list(required),
        "additionalProperties": False,
    }
