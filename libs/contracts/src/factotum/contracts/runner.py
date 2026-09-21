"""What a skill is run with, and what it gives back.

A skill in script mode is a function from the platform's point of view: its parameters go in as JSON
and ``{message, data, commands}`` comes out. The world is affected through the network it was
allowed; commands for devices leave as *data* that the platform runs after the checks, so the
sandbox needs no channel back into the platform (ADR 0006). This module is that boundary. Whatever
runs the skill (``SkillRunner``) is behind it: the isolated runner of slice 5b, or the stand-in that
only pretends.

There are two boundaries here. ``RunRequest`` and ``RunResult`` are the actions service's own: what
to run, for whom, and what came of it. ``Job`` and ``JobResult`` are what crosses to the runner
service, which knows nothing of skills, tenants or devices: files to run, what to give them, where
they may go and for how long, and what they wrote.
"""

import base64
import binascii
import re
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from factotum.contracts.base import Contract
from factotum.contracts.commands import DeviceCommand
from factotum.contracts.identity import Owner
from factotum.contracts.ids import SkillId, TenantId
from factotum.contracts.skills import (
    HOST_PATTERN,
    ParamValue,
    SkillMode,
    SkillPermissions,
    is_reserved_env_name,
    is_skill_path,
)

MAX_COMMANDS_PER_RUN = 20

MAX_JOB_FILES = 50
MAX_JOB_FILE_BYTES = 1_000_000
MAX_JOB_BYTES = 5_000_000
"""What a folder of a skill may hold (the same limits as when it is read)."""

MAX_STDIN_CHARS = 65_536
MAX_ENV_VALUE_CHARS = 8192
MAX_JOB_HOSTS = 20
MAX_OUTPUT_CHARS = 65_536
"""The most a job may write to each of its two outputs."""

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class RunRequest(Contract):
    tenant_id: TenantId
    owner: Owner
    """Whose skill it is: the secrets it may be given are theirs."""

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


class SkillOutput(Contract):
    """What a script prints, as one JSON document, when it is done. Nothing else is read from it:
    a field that is not one of these is a script that says something the platform does not hear,
    and it is told so."""

    message: str | None = Field(default=None, min_length=1, max_length=2000)
    """What to say about it. It is spoken as it is: it is not read by a model."""

    data: dict[str, Any] = Field(default_factory=dict)
    """What the skill found, for whoever asked (never spoken)."""

    commands: tuple[DeviceCommand, ...] = Field(default=(), max_length=MAX_COMMANDS_PER_RUN)
    """What the platform is to have a device do, once the checks are made."""


class RunResult(SkillOutput):
    dry_run: bool = False
    """Nothing was run: this says what would have been. A script cannot say it."""


# ---- what crosses to the runner service ---------------------------------------------------------


class JobFile(Contract):
    path: str = Field(min_length=1, max_length=200)
    """Where it is, inside the folder of the skill (with ``/``, never a way out of it)."""

    content: str
    """What it holds, in base64."""

    @field_validator("path")
    @classmethod
    def _is_a_path_inside_the_folder(cls, path: str) -> str:
        if not is_skill_path(path):
            raise ValueError("a file is in the folder of the skill, and its path says where")
        return path

    @field_validator("content")
    @classmethod
    def _is_base64_and_not_too_big(cls, content: str) -> str:
        if len(content) > MAX_JOB_FILE_BYTES * 4 // 3 + 4:
            raise ValueError(f"a file is at most {MAX_JOB_FILE_BYTES} bytes")
        try:
            base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("the content of a file is base64") from None
        return content

    @property
    def data(self) -> bytes:
        return base64.b64decode(self.content)

    @classmethod
    def of(cls, path: str, data: bytes) -> Self:
        return cls(path=path, content=base64.b64encode(data).decode("ascii"))


class Job(Contract):
    """A script to run, and everything it is given. The runner runs it and says what came of it;
    it does not know what a skill, a device or a tenant is."""

    label: str = Field(min_length=1, max_length=64)
    """What to call it in a log (the name of the skill): never a value that is somebody's."""

    files: tuple[JobFile, ...] = Field(min_length=1, max_length=MAX_JOB_FILES)
    entrypoint: str = Field(min_length=1, max_length=200)
    """The script to run: one of the files, a Python one."""

    stdin: str = Field(default="", max_length=MAX_STDIN_CHARS)
    """What the script reads on standard input (the JSON of its parameters)."""

    env: dict[str, str] = Field(default_factory=dict, max_length=20)
    """The secrets it was granted, by name."""

    hosts: tuple[str, ...] = Field(default=(), max_length=MAX_JOB_HOSTS)
    """The hosts it may reach through the proxy: a name, a name with ``*.`` before it, or an
    address. Nothing else is reachable."""

    timeout_s: int = Field(ge=1, le=120)

    @field_validator("env")
    @classmethod
    def _names_and_values_are_ones_a_program_can_be_started_with(
        cls, env: dict[str, str]
    ) -> dict[str, str]:
        for name, value in env.items():
            if not _ENV_NAME.fullmatch(name) or is_reserved_env_name(name):
                raise ValueError("a secret is given by a name in capitals that is not the runner's")
            if len(value) > MAX_ENV_VALUE_CHARS or "\x00" in value:
                raise ValueError("a secret is text, and not a long one")
        return env

    @field_validator("hosts")
    @classmethod
    def _hosts_are_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        invalid = [host for host in hosts if not HOST_PATTERN.fullmatch(host)]
        if invalid:
            raise ValueError("a host is a name, a name with *. before it, or an address")
        return tuple(dict.fromkeys(host.lower() for host in hosts))

    @model_validator(mode="after")
    def _is_a_script_that_is_there(self) -> Self:
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("a file is there once")
        if self.entrypoint not in paths:
            raise ValueError("the script to run is one of the files")
        if not self.entrypoint.endswith(".py"):
            raise ValueError("only Python scripts are run")
        if sum(len(file.data) for file in self.files) > MAX_JOB_BYTES:
            raise ValueError(f"the files are at most {MAX_JOB_BYTES} bytes in all")
        return self


class JobResult(Contract):
    """What a script did: how it ended and what it wrote. Nothing in it is trusted: it is what a
    skill said."""

    exit_code: int | None = None
    """None when it did not end by itself (it was stopped)."""

    stdout: str = Field(default="", max_length=MAX_OUTPUT_CHARS)
    stderr: str = Field(default="", max_length=MAX_OUTPUT_CHARS)
    timed_out: bool = False
    truncated: bool = False
    """It wrote more than is kept, and what is here is the start of it."""

    oom_killed: bool = False
    """It was stopped for asking for more memory than it may have."""

    duration_ms: int = Field(default=0, ge=0)
