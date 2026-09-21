"""What a skill is run with, and what it gives back.

A skill in script mode is a function from the platform's point of view: its parameters go in as JSON
and ``{message, data, commands}`` comes out. The world is affected through the network it was
allowed; commands for devices leave as *data* that the platform runs after the checks, so the
sandbox needs no channel back into the platform (ADR 0006). This module is that boundary. Whatever
runs the skill (``SkillRunner``) is behind it: the isolated runner of slice 5b, or the stand-in that
only pretends until then.
"""

from typing import Any

from pydantic import Field

from factotum.contracts.base import Contract
from factotum.contracts.commands import DeviceCommand
from factotum.contracts.ids import SkillId
from factotum.contracts.skills import ParamValue, SkillMode, SkillPermissions

MAX_COMMANDS_PER_RUN = 20


class RunRequest(Contract):
    skill_id: SkillId
    skill_name: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    mode: SkillMode
    entrypoint: str | None = None
    params: dict[str, ParamValue] = Field(default_factory=dict)
    """What the person asked for, validated against the skill's schema."""

    device_params: dict[str, ParamValue] = Field(default_factory=dict)
    """What is fixed for the device the skill was bound to (an entity id, a host...)."""

    permissions: SkillPermissions = Field(default_factory=SkillPermissions)
    """What was granted when the version was enabled: the most the skill may reach."""

    timeout_s: int = Field(ge=1, le=120)


class RunResult(Contract):
    message: str | None = Field(default=None, min_length=1, max_length=2000)
    """What to say about it. It is spoken as it is: it is not read by a model."""

    data: dict[str, Any] = Field(default_factory=dict)
    """What the skill found, for whoever asked (never spoken)."""

    commands: tuple[DeviceCommand, ...] = Field(default=(), max_length=MAX_COMMANDS_PER_RUN)
    """What the platform is to have a device do, once the checks are made."""

    dry_run: bool = False
    """Nothing was run: this says what would have been."""
