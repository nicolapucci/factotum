"""Running a skill in the runner service: what it is given, and what it answers with.

The runner knows nothing of skills: it is given files to run, what they read, the secrets they were
granted and the hosts they may reach. Everything that is about *this* skill is settled here, before
the job leaves. The files are those of the version that was enabled, read from the store and never
from what a caller sent. The secrets are the skill's owner's own and only those the version was
granted. The hosts are what the grant names, with the device's own values where it refers to them.

What comes back is what a script printed, and none of it is trusted: it is read as a
``SkillOutput``, and whatever is not one is a failure of the skill, told by what is wrong with it
and never by what it said.
"""

import json
import logging
from collections.abc import Mapping

import httpx
from pydantic import ValidationError

from factotum.actions.secrets import SecretResolver
from factotum.actions.store.base import ActionsStore
from factotum.common.devauth import PrincipalDelegation
from factotum.contracts.errors import RunnerUnavailableError, SecretNotFoundError, SkillRunError
from factotum.contracts.identity import Principal
from factotum.contracts.runner import Job, JobFile, JobResult, RunRequest, RunResult, SkillOutput
from factotum.contracts.skills import (
    HOST_PATTERN,
    ParamValue,
    SkillMode,
    SkillPermissions,
    device_template_name,
)

logger = logging.getLogger("factotum.actions.runner")

STDERR_LOGGED_CHARS = 300
"""How much of what a failed script wrote to its error output is kept in the log."""

_NOT_AVAILABLE = {401, 403, 429, 502, 503, 504}
"""What a runner that cannot be used answers with: it does not know us, it is busy, or something in
front of it is down. It is not what a script did."""


def hosts_of(
    permissions: SkillPermissions, device_params: Mapping[str, ParamValue]
) -> tuple[str, ...]:
    """The hosts that a run may reach: what the version was granted, with the device's own values
    put where the grant refers to them (``${device.host}``). A device's value is one host: it is
    never a pattern, whatever it was bound with."""
    hosts: list[str] = []
    for entry in permissions.network:
        name = device_template_name(entry)
        if name is None:
            hosts.append(entry)
            continue
        value = device_params.get(name)
        if not isinstance(value, str) or value.startswith("*") or not HOST_PATTERN.fullmatch(value):
            raise SkillRunError(f"the device has no {name} that is a host to reach")
        hosts.append(value)
    return tuple(dict.fromkeys(host.lower() for host in hosts))


def _problems(error: ValidationError) -> str:
    """What is wrong, field by field, and never what it was given: the values may be secrets."""
    return "; ".join(
        f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
        for problem in error.errors(include_input=False, include_context=False, include_url=False)
    )


def read_output(skill: str, result: JobResult) -> RunResult:
    """What a script did, as what a skill answers with. Raises ``SkillRunError`` when it did not
    end well, or when what it printed is not an answer."""
    if result.timed_out:
        raise SkillRunError("the script did not finish in time")
    if result.oom_killed:
        raise SkillRunError("the script asked for more memory than it may have")
    if result.exit_code != 0:
        logger.warning(
            "a script failed",
            extra={
                "skill": skill,
                "exit_code": result.exit_code,
                "stderr": result.stderr[-STDERR_LOGGED_CHARS:],
            },
        )
        raise SkillRunError(f"the script ended with the exit code {result.exit_code}")
    text = result.stdout.strip()
    if not text:
        return RunResult()
    try:
        answer = json.loads(text)
    except ValueError:
        raise SkillRunError("the script did not print its answer as one JSON document") from None
    try:
        output = SkillOutput.model_validate(answer)
    except ValidationError as error:
        raise SkillRunError(
            f"the answer of the script is not one a skill gives: {_problems(error)}"
        ) from None
    return RunResult(message=output.message, data=output.data, commands=output.commands)


class RemoteRunner:
    """Runs the scripts of skills in the runner service, one container each."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        store: ActionsStore,
        secrets: SecretResolver,
        delegation: PrincipalDelegation,
        margin_s: float = 10.0,
    ) -> None:
        self._client = client
        self._store = store
        self._secrets = secrets
        self._delegation = delegation
        self._margin_s = margin_s
        """How much longer than the script's own limit the runner is waited for: it makes the
        container, and takes it away."""

    async def run(self, request: RunRequest) -> RunResult:
        job = await self._job(request)
        result = await self._send(request, job)
        logger.info(
            "a skill was run",
            extra={
                "skill": request.skill_name,
                "version": request.version,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
            },
        )
        return read_output(request.skill_name, result)

    async def _job(self, request: RunRequest) -> Job:
        """What the runner is asked to run for this request."""
        if request.mode is not SkillMode.SCRIPT or request.entrypoint is None:
            raise SkillRunError("a skill that has no script is not run by the runner")
        if request.permissions.accounts:
            raise SkillRunError(
                "the skill needs a linked account, and accounts are linked by the hub, "
                "which is not there yet"
            )
        files = await self._store.get_files(request.tenant_id, request.skill_id, request.version)
        if files is None or request.entrypoint not in files:
            raise SkillRunError("the files of the version that was enabled are not there")
        try:
            secrets = await self._secrets.resolve(
                request.tenant_id, request.owner, request.permissions.secrets
            )
        except SecretNotFoundError as error:
            raise SkillRunError(error.message) from None
        stdin = json.dumps({"params": request.params, "device_params": request.device_params})
        try:
            return Job(
                label=request.skill_name,
                files=tuple(JobFile.of(path, data) for path, data in sorted(files.items())),
                entrypoint=request.entrypoint,
                stdin=stdin,
                env=secrets,
                hosts=hosts_of(request.permissions, request.device_params),
                timeout_s=request.timeout_s,
            )
        except ValidationError as error:
            raise SkillRunError(
                f"the skill cannot be given to the runner: {_problems(error)}"
            ) from None

    async def _send(self, request: RunRequest, job: Job) -> JobResult:
        headers = dict(self._delegation.headers(Principal.of_tenant(request.tenant_id)))
        headers["content-type"] = "application/json"
        timeout = httpx.Timeout(
            connect=3.0, read=job.timeout_s + self._margin_s, write=10.0, pool=5.0
        )
        try:
            response = await self._client.post(
                "/v1/jobs", content=job.model_dump_json(), headers=headers, timeout=timeout
            )
        except httpx.HTTPError:
            raise RunnerUnavailableError("the runner could not be reached") from None
        if response.status_code == 200:
            try:
                return JobResult.model_validate_json(response.content)
            except ValidationError:
                raise RunnerUnavailableError(
                    "the runner answered with something that is not the result of a job"
                ) from None
        if response.status_code in _NOT_AVAILABLE:
            raise RunnerUnavailableError(f"the runner cannot be used ({response.status_code})")
        raise SkillRunError(f"the runner refused the job ({response.status_code})")
