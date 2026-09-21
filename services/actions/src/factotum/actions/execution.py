"""Running a skill, and having a device do what it answered with.

Two interfaces, and what stands in for them until slice 5b. ``SkillRunner`` runs a skill (a script
in an isolated container, in 5b) and answers ``{message, data, commands}``; ``CommandDispatcher``
gives a command to a device and says what became of it (over the edge, in 5b). Everything the
service does before and after them is real: only the two ends are stand-ins, and what stands in
says so.

The stand-in for the runner does not run anything: it says what it would have run, and the answer
is marked as a dry run all the way to the audit trail, where it is *not executed*. The stand-in for
the dispatcher does not reach a device: a command is not done.
"""

from typing import Protocol

from factotum.contracts.commands import CommandResult, CommandStatus, DeviceCommand
from factotum.contracts.ids import DeviceId, TenantId
from factotum.contracts.runner import RunRequest, RunResult


class SkillRunner(Protocol):
    async def run(self, request: RunRequest) -> RunResult:
        """Run a version of a skill with what it was asked with. Raises ``SkillRunError`` when the
        skill failed or took too long, and ``RunnerUnavailableError`` when nothing can run it."""
        ...


class CommandDispatcher(Protocol):
    async def dispatch(
        self, tenant_id: TenantId, device_id: DeviceId, command: DeviceCommand
    ) -> CommandResult:
        """Have a system device run a command that was checked, and say what became of it."""
        ...


class DryRunRunner:
    """Says what would have been run, and runs nothing."""

    async def run(self, request: RunRequest) -> RunResult:
        given = ", ".join(f"{name}={value}" for name, value in sorted(request.params.items()))
        return RunResult(
            message=f"Dry run: {request.skill_name}({given})",
            data={"skill": request.skill_name, "version": request.version},
            dry_run=True,
        )


class NullDispatcher:
    """A dispatcher that reaches no device: what it is given is not done."""

    async def dispatch(
        self, tenant_id: TenantId, device_id: DeviceId, command: DeviceCommand
    ) -> CommandResult:
        return CommandResult(
            status=CommandStatus.FAILED, detail="commands are not dispatched to devices yet"
        )
