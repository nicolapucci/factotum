"""What a plan may do: decided in code, from what the skill was granted.

The model proposes and the code decides (ADR 0014). A plan is somebody's words turned into requests,
and words can be wrong or can have been steered, so before anything is sent every step is checked
against what the version of the skill that runs was granted, which a reviewer looked at and enabled:

* a request goes to a host that the skill may reach, and to none other (the same rule as the proxy
  that the sandbox goes out through, because it is the same function), with no user name or password
  in the address: what proves who asks goes in a header;
* a skill that only reads (``effect: read_only``) only reads: its requests are ``GET``s,
  whatever the instructions said or whoever asked;
* a secret is one the skill was granted, and it is in a request and nowhere else: it is not said and
  it is not given to a device;
* what is put in from a value of the device or from what the person asked for is something that
  exists, and what is put in from an earlier step is a step that was done before;
* a command is one of the vocabulary, with the arguments it has.

A plan that fails any of this is not run, and the message says which step and what, in words a model
can act on (it is given them once, to write the plan again) and a person can read (they are in the
trail). It never says what a secret is: a plan holds none.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import TypeAdapter, ValidationError

from factotum.actions.instructions.executor import (
    Context,
    LanguageError,
    Ref,
    find_refs,
    names_of,
    split_url,
    strings_of,
    substitute_text,
)
from factotum.common.hosts import match_host, normalize_host
from factotum.contracts.commands import SKILL_COMMAND_MODELS, BaseCommand, SkillCommand
from factotum.contracts.effects import Effect
from factotum.contracts.instructions import InstructionPlan, PlanStep
from factotum.contracts.skills import HOST_PATTERN, ParamValue, SkillManifest, SkillPermissions

MAX_REASON_CHARS = 300

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "upgrade"}
    | {"te", "trailer", "expect", "proxy-authorization", "proxy-authenticate", "proxy-connection"}
)
"""What is not the plan's to say: what the program that runs it says itself, or what would change
what a request is."""

_COMMANDS: dict[str, type[BaseCommand]] = {
    str(model.model_fields["name"].default): model for model in SKILL_COMMAND_MODELS
}
_COMMAND: TypeAdapter[SkillCommand] = TypeAdapter(SkillCommand)


class PlanRejected(Exception):  # noqa: N818 - it says what the checks did with the plan
    """The plan is not run as it is written. The message says which step and what."""

    def __init__(self, why: str) -> None:
        text = " ".join(why.split())
        super().__init__(
            text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 1] + "…"
        )


def _clipped(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def command_names() -> tuple[str, ...]:
    """The commands a plan may give a device: the vocabulary of skills, and nothing else."""
    return tuple(sorted(_COMMANDS))


class _Rules:
    """What one plan is checked against."""

    def __init__(
        self,
        *,
        manifest: SkillManifest,
        granted: SkillPermissions,
        hosts: Sequence[str],
        values: Mapping[str, ParamValue],
        device_params: Mapping[str, ParamValue],
    ) -> None:
        self.effect = manifest.effect
        self.parameters = frozenset(manifest.parameters)
        self.secrets = frozenset(granted.secrets)
        self.hosts = tuple(hosts)
        self.values = values
        self.device_params = device_params
        self.earlier: list[str] = []

    # ---- a placeholder ------------------------------------------------------------------------

    def refs(self, text: str, where: str) -> list[Ref]:
        try:
            return find_refs(text)
        except LanguageError as error:
            raise PlanRejected(f"{where}: {error}") from None

    def check_ref(self, ref: Ref, where: str, *, secret_allowed: bool) -> None:
        text = _clipped(ref.raw)
        if ref.kind == "secret":
            if not secret_allowed:
                raise PlanRejected(
                    f"{where}: {text}: a secret is put in a request and nowhere else"
                )
            if ref.name not in self.secrets:
                raise PlanRejected(f"{where}: {text}: the skill was not granted that secret")
        elif ref.kind == "device":
            if ref.name not in self.device_params:
                have = ", ".join(sorted(self.device_params)) or "none"
                raise PlanRejected(
                    f"{where}: {text}: the device has no such value (it has: {have})"
                )
        elif ref.kind == "value":
            if ref.name not in self.values:
                why = "was not given" if ref.name in self.parameters else "is not one of its values"
                raise PlanRejected(f"{where}: {text}: that value {why}")
            given = self.values[ref.name]
            if ref.filter == "round" and (
                isinstance(given, bool) or not isinstance(given, int | float)
            ):
                raise PlanRejected(f"{where}: {text}: only a number is rounded")
        elif ref.name not in self.earlier:
            done = ", ".join(self.earlier) or "none yet"
            raise PlanRejected(f"{where}: {text}: that is not a request done before (done: {done})")

    def check_text(self, text: str, where: str, *, secret_allowed: bool) -> None:
        for ref in self.refs(text, where):
            self.check_ref(ref, where, secret_allowed=secret_allowed)

    def check_json(self, value: Any, where: str, *, secret_allowed: bool) -> None:
        if any("${" in name for name in names_of(value)):
            raise PlanRejected(f"{where}: the name of a field is not a place for a placeholder")
        for text in strings_of(value):
            self.check_text(text, where, secret_allowed=secret_allowed)

    # ---- a request ----------------------------------------------------------------------------

    def check_request(self, step: PlanStep) -> None:
        where = f"step {step.id}"
        assert step.method is not None  # noqa: S101 - the contract says so
        assert step.url is not None  # noqa: S101
        if self.effect is Effect.READ_ONLY and step.method != "GET":
            raise PlanRejected(
                f"{where}: the skill only reads, so its requests are GET, and this one is "
                f"{step.method}"
            )
        self.check_address(step.url, where)
        for name, value in step.headers.items():
            self.check_header(name, value, where)
        if step.body is not None:
            if step.method == "GET":
                raise PlanRejected(f"{where}: a GET has no body")
            if isinstance(step.body, str):
                self.check_text(step.body, where, secret_allowed=True)
            else:
                self.check_json(step.body, where, secret_allowed=True)

    def check_address(self, url: str, where: str) -> None:
        parts = split_url(url)
        if parts is None or parts[0].lower() not in {"http", "https"}:
            raise PlanRejected(f"{where}: the address is a full http or https address")
        _, authority, rest = parts
        if "@" in authority:
            raise PlanRejected(
                f"{where}: the address has a user name or password; what proves who asks goes in "
                "a header"
            )
        for ref in self.refs(authority, where):
            if ref.kind != "device":
                raise PlanRejected(
                    f"{where}: {_clipped(ref.raw)}: the host of an address is fixed; only a value "
                    "of the device can be in it"
                )
            self.check_ref(ref, where, secret_allowed=False)
        self.check_text(rest, where, secret_allowed=True)
        try:
            written = substitute_text(
                authority, Context(secrets={}, device=self.device_params, values={})
            )
        except LanguageError as error:
            raise PlanRejected(f"{where}: {error}") from None
        host, _, port = written.rpartition(":") if ":" in written else (written, "", "")
        if written.startswith("[") or (port and not (port.isascii() and port.isdigit())):
            raise PlanRejected(
                f"{where}: the host {_clipped(written)} is a name or an IPv4 address"
            )
        if port and not 1 <= int(port) <= 65535:
            raise PlanRejected(f"{where}: {port} is not a port")
        host = normalize_host(host)
        if not host or not HOST_PATTERN.fullmatch(host):
            raise PlanRejected(f"{where}: {_clipped(written)} is not a host")
        if match_host(self.hosts, host) is None:
            may = ", ".join(self.hosts) or "none"
            raise PlanRejected(
                f"{where}: the host {_clipped(host)} is not one the skill may reach (it may "
                f"reach: {may})"
            )

    def check_header(self, name: str, value: str, where: str) -> None:
        lowered = name.lower()
        if (
            not _HEADER_NAME.fullmatch(name)
            or lowered in _FORBIDDEN_HEADERS
            or lowered.startswith("proxy-")
        ):
            raise PlanRejected(f"{where}: the header {_clipped(name, 30)} is not the plan's to set")
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
            raise PlanRejected(f"{where}: the header {_clipped(name, 30)} has a control character")
        self.check_text(value, where, secret_allowed=True)

    # ---- a command ----------------------------------------------------------------------------

    def check_command(self, step: PlanStep) -> None:
        where = f"step {step.id}"
        assert step.name is not None  # noqa: S101 - the contract says so
        model = _COMMANDS.get(step.name)
        if model is None:
            raise PlanRejected(
                f"{where}: {_clipped(step.name, 30)} is not a command (they are: "
                f"{', '.join(command_names())})"
            )
        allowed = set(model.model_fields) - {"name"}
        odd = sorted(set(step.args) - allowed)
        if odd:
            raise PlanRejected(
                f"{where}: {step.name} does not take an argument called {_clipped(odd[0], 30)}: "
                f"leave it out (it takes: {', '.join(sorted(allowed)) or 'none'})"
            )
        self.check_json(step.args, where, secret_allowed=False)
        if not any("${" in text for text in strings_of(step.args)):
            try:
                _COMMAND.validate_python({"name": step.name, **step.args})
            except ValidationError as error:
                fields = "; ".join(
                    f"{'.'.join(str(part) for part in problem['loc'][1:])}: {problem['msg']}"
                    for problem in error.errors(
                        include_input=False, include_context=False, include_url=False
                    )
                )
                raise PlanRejected(
                    f"{where}: {_clipped(step.name, 30)} does not take that: {fields}"
                ) from None


def check_plan(
    plan: InstructionPlan,
    *,
    manifest: SkillManifest,
    granted: SkillPermissions,
    hosts: Sequence[str],
    values: Mapping[str, ParamValue],
    device_params: Mapping[str, ParamValue],
) -> None:
    """Raises ``PlanRejected`` when the plan may not be run.

    ``hosts`` are the hosts the skill may reach (what it was granted, with the host of the device
    where the grant says ``${device.host}``), ``values`` what the person asked for and
    ``device_params`` what is fixed for the device: everything a placeholder can refer to that is
    known before the plan runs."""
    if not plan.steps:
        raise PlanRejected("the plan does nothing: it has no steps")
    rules = _Rules(
        manifest=manifest, granted=granted, hosts=hosts, values=values, device_params=device_params
    )
    for step in plan.steps:
        if step.kind == "http":
            rules.check_request(step)
            rules.earlier.append(step.id)
        else:
            rules.check_command(step)
    if plan.say is not None:
        rules.check_text(plan.say, "what to say", secret_allowed=False)


def describe(plan: InstructionPlan) -> str:
    """What a plan does, for the trail: the method and the host of each request and the name of
    each command. Placeholders are shown as they are written, and a plan has no secret."""
    parts: list[str] = []
    for step in plan.steps:
        if step.kind == "http":
            pieces = split_url(step.url or "")
            parts.append(f"{step.method} {_clipped(pieces[1], 40) if pieces else '?'}")
        else:
            parts.append(str(step.name))
    return _clipped(", ".join(parts), 200) or "nothing"
