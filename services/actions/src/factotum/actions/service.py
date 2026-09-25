"""From a message to an outcome: choosing, deciding, confirming, approving, running, auditing.

A message comes in for a person on a device. If that person was asked a yes or a no on that device
a moment ago, the message is read as the answer (``confirmations``). Otherwise the skills that may
be what it asks for are found (``matching``) and a small model proposes one (``planner``); what it
proposes is checked in code (``understanding``), authorized with the policy of the device that was
spoken to (``authorize``), and then, by the decision:

* **deny**: nothing is done, and it is audited;
* **approval needed**: an out-of-band approver is asked, and while there is none (ADR 0007) the
  action is refused with a plain reason;
* **confirmation needed**: the person is asked a yes or a no, what was understood is kept, and the
  next message on that device answers it;
* **allow**: the skill is run and the commands it answers with are given to the device.

Whatever the outcome, an action that got as far as a decision leaves one record in the audit trail
per step (asked, declined, refused, executed), and a record never says that something ran without
the confirmation or the approval its decision required.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError

from factotum.actions.approvals import NullApprover
from factotum.actions.confirmations import Answer, ConfirmationReader
from factotum.actions.directory import BoundDevice, DeviceDirectory
from factotum.actions.execution import CommandDispatcher, NullDispatcher, SkillRunner
from factotum.actions.instructions.checks import PlanRejected, check_plan, describe
from factotum.actions.instructions.writer import PlanInputs, PlanWriter
from factotum.actions.matching import Candidate, Candidates, SkillMatcher
from factotum.actions.phrases import Say, refusal, say
from factotum.actions.planner import PlanDevices, Planner, view_of
from factotum.actions.proposal import Proposal
from factotum.actions.runner_client import hosts_of
from factotum.actions.store import ActionsStore, PendingRow
from factotum.actions.understanding import NeedsMore, NothingToDo, Understood, understand
from factotum.common.authorization import authorize
from factotum.common.redaction import redact
from factotum.contracts.actions import MAX_REPLY_CHARS, ActCode, ActOutcome, ActRequest, ActStatus
from factotum.contracts.approval import (
    ActionSummary,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalResult,
    Approver,
    ApproverSelector,
)
from factotum.contracts.audit import ActionResult, ApprovalTrail, AuditRecord
from factotum.contracts.authorization import (
    ActionRequest,
    Decision,
    DecisionOutcome,
    SkillRef,
    TargetRef,
)
from factotum.contracts.commands import CommandResult, CommandStatus
from factotum.contracts.devices import ExternalDeviceRecord, SystemDeviceRecord
from factotum.contracts.errors import FactotumError, RunnerUnavailableError, SkillRunError
from factotum.contracts.identity import Owner, Principal
from factotum.contracts.ids import ApprovalId, AuditId, DeviceId, SkillId
from factotum.contracts.instructions import InstructionPlan
from factotum.contracts.runner import RunRequest, RunResult
from factotum.contracts.skills import ParamValue, SkillMode, SkillPermissions, SkillTarget

logger = logging.getLogger("factotum.actions")

MAX_FAILURE_DETAIL_CHARS = 300
"""How much of why a skill failed goes in the trail."""


@dataclass(frozen=True, slots=True)
class ActionSettings:
    confirmation_ttl: timedelta = field(default_factory=lambda: timedelta(seconds=60))
    """How long a yes or a no is waited for. What was understood is not kept longer: a yes that
    comes after a while is a yes to something else."""

    approval_wait_s: float = 20.0
    """How long a turn waits for an approver. An approval that takes longer than a person waits for
    an answer needs the hub (slice 7): until then it is an approval that did not come."""

    approval_ttl: timedelta = field(default_factory=lambda: timedelta(minutes=10))
    """How long an approval request is open, as the approver is told."""

    run_margin_s: float = 5.0
    """What is added to the time limit of a skill for everything that is not the skill."""

    max_devices_shown: int = 10
    """How many other devices the model is shown by name."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _redacted(values: dict[str, ParamValue]) -> dict[str, ParamValue]:
    """The values of an action as the trail keeps them: what looks like a secret is masked."""
    masked: dict[str, ParamValue] = redact(dict(values))
    return masked


def _with_plan(detail: str | None, plan: InstructionPlan | None) -> str | None:
    """What the trail says, and what the plan comes to when there is one: the method and host of
    each request and the name of each command, as they were written."""
    if plan is None:
        return detail
    return f"{detail}; plan: {describe(plan)}" if detail else f"plan: {describe(plan)}"


def _kind_of(target: SystemDeviceRecord | ExternalDeviceRecord | None) -> str | None:
    if target is None:
        return None
    return "system" if isinstance(target, SystemDeviceRecord) else "external"


class ActionService:
    def __init__(
        self,
        *,
        store: ActionsStore,
        directory: DeviceDirectory,
        matcher: SkillMatcher,
        planner: Planner,
        reader: ConfirmationReader,
        runner: SkillRunner,
        dispatcher: CommandDispatcher | None = None,
        writer: PlanWriter | None = None,
        approver: Approver | None = None,
        settings: ActionSettings | None = None,
        now: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._directory = directory
        self._matcher = matcher
        self._planner = planner
        self._reader = reader
        self._runner = runner
        self._dispatcher = dispatcher or NullDispatcher()
        self._writer = writer
        self._approver = approver or NullApprover()
        self._settings = settings or ActionSettings()
        self._now = now
        self._new_id = new_id

    async def act(self, principal: Principal, request: ActRequest) -> ActOutcome:
        """What a message asks for, and what became of it. A message that asks for none of the
        skills is ``NO_ACTION``: it is to be answered as a question."""
        now = self._now()
        pending = await self._store.get_pending(
            principal.tenant_id, principal.acts_as, principal.device_id
        )
        if pending is not None:
            answered = await self._answer(principal, request, pending, now)
            if answered is not None:
                return answered
        return await self._new(principal, request, now)

    # ---- a yes or a no ------------------------------------------------------------------------

    async def _answer(
        self, principal: Principal, request: ActRequest, pending: PendingRow, now: datetime
    ) -> ActOutcome | None:
        """The answer to a confirmation that was waiting, or nothing when the message is not one
        (it is then a new message, and what was waiting is dropped)."""
        try:
            proposal = Proposal.model_validate(pending.proposal)
        except ValidationError:
            await self._store.take_pending(principal.tenant_id, pending.id)
            logger.warning("a confirmation that could not be read was dropped")
            return None
        if pending.expires_at <= now:
            await self._lapse(principal, pending, proposal, "the confirmation expired")
            return None
        question = proposal.question or say(
            proposal.language, Say.CONFIRM, skill=proposal.skill_name
        )
        answer = await self._reader.read(
            Owner.of_subject(principal.acts_as), question, request.message
        )
        if answer is Answer.OTHER:
            await self._lapse(principal, pending, proposal, "something else was said instead")
            return None
        if await self._store.take_pending(principal.tenant_id, pending.id) is None:
            return None  # answered already, by whoever was quicker
        if answer is Answer.NO:
            record = await self._record(
                principal,
                skill_id=proposal.skill_id,
                skill_name=proposal.skill_name,
                target=proposal.target_id,
                values=proposal.values,
                decision=proposal.decision,
                result=ActionResult.NOT_EXECUTED,
                detail="the person declined",
                confirmed=False,
            )
            return ActOutcome(
                status=ActStatus.CANCELLED,
                reply=say(proposal.language, Say.CANCELLED),
                code=ActCode.DECLINED,
                skill=proposal.skill_name,
                audit_id=record,
            )
        return await self._confirmed(principal, proposal, now)

    async def _lapse(
        self, principal: Principal, pending: PendingRow, proposal: Proposal, why: str
    ) -> None:
        """What was waiting for a yes is dropped, and the trail says it was never confirmed."""
        if await self._store.take_pending(principal.tenant_id, pending.id) is not None:
            await self._record(
                principal,
                skill_id=proposal.skill_id,
                skill_name=proposal.skill_name,
                target=proposal.target_id,
                values=proposal.values,
                decision=proposal.decision,
                result=ActionResult.NOT_EXECUTED,
                detail=why,
                confirmed=False,
            )

    async def _confirmed(
        self, principal: Principal, proposal: Proposal, now: datetime
    ) -> ActOutcome:
        """A yes to what was asked. Nothing that was true then is taken for granted: the skill may
        have been changed or switched off, the device removed, the policy tightened."""
        stored = await self._store.get_skill(principal.tenant_id, proposal.skill_id)
        target = await self._target_of(principal, proposal)
        if (
            stored is None
            or stored.enabled is None
            or stored.enabled.meta.version != proposal.version
            or (proposal.target_id is not None and target is None)
        ):
            record = await self._record(
                principal,
                skill_id=proposal.skill_id,
                skill_name=proposal.skill_name,
                target=proposal.target_id,
                values=proposal.values,
                decision=proposal.decision,
                result=ActionResult.NOT_EXECUTED,
                detail="what was confirmed is not available any more",
                confirmed=True,
            )
            return ActOutcome(
                status=ActStatus.REFUSED,
                reply=say(proposal.language, Say.NOT_ENABLED),
                code=ActCode.SKILL_NOT_ENABLED,
                skill=proposal.skill_name,
                audit_id=record,
            )
        understood = Understood(
            candidate=Candidate(stored),
            values=proposal.values,
            device_params=proposal.device_params,
            target=target,
            language=proposal.language,
            question=proposal.question,
        )
        return await self._proceed(
            principal,
            understood,
            untrusted=proposal.untrusted_content_in_turn,
            confirmed=True,
            now=now,
            plan=proposal.plan,
        )

    async def _target_of(
        self, principal: Principal, proposal: Proposal
    ) -> SystemDeviceRecord | ExternalDeviceRecord | None:
        if proposal.target_id is None:
            return None
        if proposal.target_kind == "system":
            return await self._directory.system_device(principal.tenant_id, proposal.target_id)
        bound = await self._directory.bound_devices(principal, proposal.skill_name)
        return next((b.device for b in bound if b.device.id == proposal.target_id), None)

    # ---- a new message ------------------------------------------------------------------------

    async def _new(self, principal: Principal, request: ActRequest, now: datetime) -> ActOutcome:
        candidates = await self._matcher.candidates(principal, request.message)
        if not candidates.found:
            return ActOutcome.no_action()
        here, systems, bound = await self._context(principal, candidates)
        views = [
            view_of(c.skill.enabled.manifest, [b.device for b in bound.get(c.skill.name, [])])
            for c in candidates.found
            if c.skill.enabled is not None
        ]
        try:
            plan = await self._planner.plan(
                Owner.of_subject(principal.acts_as),
                request.message,
                views,
                self._shown(here, systems),
            )
        except FactotumError as error:
            # A message that cannot be read as an action is answered as anything else is: nothing
            # runs on a guess, and a question is never held up by a model that is away.
            logger.warning("a message could not be planned", extra={"code": error.code})
            return ActOutcome.not_read()

        understanding = understand(plan, candidates.found, here=here, systems=systems, bound=bound)
        if isinstance(understanding, NothingToDo):
            return ActOutcome.no_action()
        if isinstance(understanding, NeedsMore):
            return ActOutcome(
                status=ActStatus.NEEDS_MORE,
                reply=understanding.reply,
                code=understanding.code,
                skill=understanding.skill,
            )
        return await self._proceed(
            principal,
            understanding,
            untrusted=request.untrusted_content_in_turn,
            confirmed=False,
            now=now,
        )

    async def _context(
        self, principal: Principal, candidates: Candidates
    ) -> tuple[SystemDeviceRecord | None, list[SystemDeviceRecord], dict[str, list[BoundDevice]]]:
        here = await self._here(principal)
        systems = await self._directory.system_devices(principal)
        bound: dict[str, list[BoundDevice]] = {}
        for candidate in candidates.found:
            enabled = candidate.skill.enabled
            if enabled is not None and enabled.manifest.target is SkillTarget.EXTERNAL:
                bound[candidate.skill.name] = await self._directory.bound_devices(
                    principal, candidate.skill.name
                )
        return here, systems, bound

    async def _here(self, principal: Principal) -> SystemDeviceRecord | None:
        if principal.device_id is None:
            return None
        return await self._directory.system_device(principal.tenant_id, principal.device_id)

    def _shown(
        self, here: SystemDeviceRecord | None, systems: list[SystemDeviceRecord]
    ) -> PlanDevices:
        others = [d.name for d in systems if here is None or d.id != here.id]
        return PlanDevices(
            here=None if here is None else here.name,
            others=tuple(others[: self._settings.max_devices_shown]),
        )

    # ---- the decision -------------------------------------------------------------------------

    async def _proceed(
        self,
        principal: Principal,
        understood: Understood,
        *,
        untrusted: bool,
        confirmed: bool,
        now: datetime,
        plan: InstructionPlan | None = None,
    ) -> ActOutcome:
        stored = understood.candidate.skill
        assert stored.enabled is not None  # noqa: S101 - only skills that run are offered
        manifest = stored.enabled.manifest
        here = await self._here(principal)
        said = confirmed or None
        if principal.device_id is not None and here is None:
            # A device nobody knows is not one that anything is done for.
            return await self._settle(
                principal,
                understood,
                Decision(outcome=DecisionOutcome.DENY, effective_effect=manifest.effect),
                status=ActStatus.REFUSED,
                code=ActCode.UNKNOWN_DEVICE,
                reply=say(understood.language, Say.UNKNOWN_DEVICE),
                result=ActionResult.NOT_EXECUTED,
                detail="the device that asked is not known",
                confirmed=said,
            )
        decision = authorize(
            principal,
            None if here is None else here.policy,
            ActionRequest(
                skill=SkillRef.from_manifest(
                    manifest, skill_id=stored.id, tenant_id=stored.tenant_id, owner=stored.owner
                ),
                targets=(
                    () if understood.target is None else (TargetRef.from_record(understood.target),)
                ),
                untrusted_content_in_turn=untrusted,
            ),
        )

        if decision.outcome is DecisionOutcome.DENY:
            return await self._settle(
                principal,
                understood,
                decision,
                status=ActStatus.REFUSED,
                code=ActCode.DENIED,
                reply=refusal(understood.language, decision.reasons),
                result=ActionResult.NOT_EXECUTED,
                detail="refused by the policy",
                confirmed=said,
            )
        if decision.outcome is DecisionOutcome.REQUIRE_APPROVAL:
            return await self._approve(principal, understood, decision, said)
        if decision.outcome is DecisionOutcome.REQUIRE_CONFIRMATION:
            if not confirmed:
                return await self._ask(principal, understood, decision, untrusted, now)
            return await self._run(
                principal, understood, decision, confirmed=True, approval=None, plan=plan
            )
        return await self._run(principal, understood, decision, confirmed=said, approval=None)

    async def _ask(
        self,
        principal: Principal,
        understood: Understood,
        decision: Decision,
        untrusted: bool,
        now: datetime,
    ) -> ActOutcome:
        """Ask the person a yes or a no, and keep what is asked until it is answered. For a skill
        that is instructions, what is asked is what a model wrote for it: the plan is written first,
        checked, and kept with the question, so that a yes confirms what will be done and nothing
        else is written after it."""
        stored = understood.candidate.skill
        assert stored.enabled is not None  # noqa: S101
        planned = await self._planned(principal, understood, decision, plan=None, confirmed=None)
        if isinstance(planned, ActOutcome):
            return planned
        proposal = Proposal(
            skill_id=stored.id,
            skill_name=stored.name,
            version=stored.enabled.meta.version,
            values=understood.values,
            device_params=understood.device_params,
            target_id=None if understood.target is None else understood.target.id,
            target_kind=_kind_of(understood.target),  # type: ignore[arg-type]
            language=understood.language,
            question=understood.question,
            decision=decision,
            untrusted_content_in_turn=untrusted,
            plan=planned,
        )
        await self._store.put_pending(
            PendingRow(
                id=self._new_id(),
                tenant_id=principal.tenant_id,
                subject=principal.acts_as,
                device_id=principal.device_id,
                proposal=proposal.model_dump(mode="json"),
                created_at=now,
                expires_at=now + self._settings.confirmation_ttl,
            )
        )
        return await self._settle(
            principal,
            understood,
            decision,
            status=ActStatus.NEEDS_CONFIRMATION,
            code=None,
            reply=understood.question or say(understood.language, Say.CONFIRM, skill=stored.name),
            result=ActionResult.NOT_EXECUTED,
            detail=_with_plan("asked for confirmation", planned),
            confirmed=None,
        )

    async def _approve(
        self,
        principal: Principal,
        understood: Understood,
        decision: Decision,
        confirmed: bool | None,
    ) -> ActOutcome:
        """Ask an authorized person, out of band. A person who said yes has not been approved:
        a yes authenticates nobody (ADR 0004)."""
        stored = understood.candidate.skill
        requested = self._now()
        request = ApprovalRequest(
            id=ApprovalId(self._new_id()),
            tenant_id=principal.tenant_id,
            acts_as=principal.acts_as,
            device_id=principal.device_id,
            action=ActionSummary(
                skill_id=stored.id,
                skill_name=stored.name,
                targets=() if understood.target is None else (understood.target.id,),
                params=_redacted(understood.values),
            ),
            risk=decision.effective_effect,
            reasons=decision.reasons,
            approvers=ApproverSelector.for_subject(principal.acts_as),
            requested_at=requested,
            expires_at=requested + self._settings.approval_ttl,
        )
        try:
            async with asyncio.timeout(self._settings.approval_wait_s):
                result = await self._approver.request_approval(request)
        except TimeoutError:
            result = ApprovalResult(outcome=ApprovalOutcome.EXPIRED)
        except FactotumError:
            result = ApprovalResult.unavailable()
        trail = ApprovalTrail(
            outcome=result.outcome,
            approved_by=result.approved_by,
            requested_at=requested,
            resolved_at=max(self._now(), requested),
        )
        if result.outcome is ApprovalOutcome.APPROVED:
            return await self._run(
                principal, understood, decision, confirmed=confirmed, approval=trail
            )
        code, key, why = {
            ApprovalOutcome.DENIED: (ActCode.APPROVAL_DENIED, Say.APPROVAL_DENIED, "denied"),
            ApprovalOutcome.EXPIRED: (ActCode.APPROVAL_EXPIRED, Say.APPROVAL_EXPIRED, "expired"),
        }.get(
            result.outcome,
            (ActCode.APPROVAL_UNAVAILABLE, Say.APPROVAL_UNAVAILABLE, "there is no way to ask"),
        )
        return await self._settle(
            principal,
            understood,
            decision,
            status=ActStatus.REFUSED,
            code=code,
            reply=say(understood.language, key),
            result=ActionResult.NOT_EXECUTED,
            detail=f"not approved: {why}",
            confirmed=confirmed,
            approval=trail,
        )

    # ---- doing it -----------------------------------------------------------------------------

    async def _run(
        self,
        principal: Principal,
        understood: Understood,
        decision: Decision,
        *,
        confirmed: bool | None,
        approval: ApprovalTrail | None,
        plan: InstructionPlan | None = None,
    ) -> ActOutcome:
        stored = understood.candidate.skill
        assert stored.enabled is not None  # noqa: S101
        manifest = stored.enabled.manifest
        language = understood.language
        planned = await self._planned(
            principal, understood, decision, plan=plan, confirmed=confirmed, approval=approval
        )
        if isinstance(planned, ActOutcome):
            return planned
        plan = planned

        async def settle(
            status: ActStatus,
            code: ActCode | None,
            reply: str,
            result: ActionResult,
            detail: str | None,
        ) -> ActOutcome:
            return await self._settle(
                principal,
                understood,
                decision,
                status=status,
                code=code,
                reply=reply,
                result=result,
                detail=detail,
                confirmed=confirmed,
                approval=approval,
            )

        request = RunRequest(
            tenant_id=stored.tenant_id,
            owner=stored.owner,
            skill_id=stored.id,
            skill_name=stored.name,
            version=stored.enabled.meta.version,
            mode=manifest.mode,
            entrypoint=manifest.entrypoint,
            params=understood.values,
            device_params=understood.device_params,
            permissions=stored.enabled.granted or SkillPermissions(),
            timeout_s=manifest.timeout_s,
            plan=plan,
        )
        try:
            async with asyncio.timeout(manifest.timeout_s + self._settings.run_margin_s):
                result = await self._runner.run(request)
        except RunnerUnavailableError as error:
            logger.warning(
                "the runner could not run a skill",
                extra={"skill": stored.name, "reason": error.message},
            )
            return await settle(
                ActStatus.FAILED,
                ActCode.RUNNER_UNAVAILABLE,
                say(language, Say.RUNNER_UNAVAILABLE),
                ActionResult.FAILED,
                "nothing can run skills",
            )
        except Exception as error:
            # What a run says of itself is ours (which secret, which code, which status) and never
            # what a script wrote; anything else is what kind of thing it was.
            reason = error.message if isinstance(error, SkillRunError) else type(error).__name__
            if isinstance(error, FactotumError | TimeoutError):
                logger.warning("a skill failed", extra={"skill": stored.name, "reason": reason})
            else:
                logger.exception("a runner failed", extra={"skill": stored.name})
            return await settle(
                ActStatus.FAILED,
                ActCode.SKILL_FAILED,
                say(language, Say.FAILED),
                ActionResult.FAILED,
                f"the skill failed: {reason}"[:MAX_FAILURE_DETAIL_CHARS],
            )

        if result.dry_run:
            return await settle(
                ActStatus.DONE,
                None,
                say(language, Say.DRY_RUN, skill=stored.name),
                ActionResult.NOT_EXECUTED,
                "dry run: nothing was run",
            )
        given = await self._give(principal, understood, result)
        failed = next((outcome for outcome in given if not outcome.succeeded), None)
        if failed is not None:
            return await settle(
                ActStatus.FAILED,
                ActCode.COMMAND_FAILED,
                say(language, Say.COMMAND_FAILED),
                ActionResult.FAILED,
                f"a command was not done ({failed.status.value})",
            )
        return await settle(
            ActStatus.DONE,
            None,
            _clip(result.message, MAX_REPLY_CHARS) if result.message else say(language, Say.DONE),
            ActionResult.EXECUTED,
            _with_plan(None, plan),
        )

    async def _planned(
        self,
        principal: Principal,
        understood: Understood,
        decision: Decision,
        *,
        plan: InstructionPlan | None,
        confirmed: bool | None,
        approval: ApprovalTrail | None = None,
    ) -> InstructionPlan | ActOutcome | None:
        """The plan for a skill that is instructions: the one that was confirmed, checked again, or
        one that a model writes now. Nothing for a skill that has a script, or when nothing is set
        up to write one (the skill is then run as it is, which is a dry run). An outcome, which is
        the answer to give, when there is no plan that may be run."""
        stored = understood.candidate.skill
        assert stored.enabled is not None  # noqa: S101
        manifest = stored.enabled.manifest
        if manifest.mode is not SkillMode.INSTRUCTIONS or self._writer is None:
            return None
        granted = stored.enabled.granted or SkillPermissions()
        try:
            if not stored.enabled.instructions.strip():
                raise PlanRejected("the skill has no instructions to follow")
            hosts = hosts_of(granted, understood.device_params)

            def check(candidate: InstructionPlan) -> None:
                check_plan(
                    candidate,
                    manifest=manifest,
                    granted=granted,
                    hosts=hosts,
                    values=understood.values,
                    device_params=understood.device_params,
                )

            if plan is not None:
                check(plan)
                return plan
            return await self._writer.write(
                Owner.of_subject(principal.acts_as),
                PlanInputs(
                    name=stored.name,
                    description=manifest.description,
                    instructions=stored.enabled.instructions,
                    effect=manifest.effect,
                    values=understood.values,
                    device_values=tuple(understood.device_params),
                    hosts=hosts,
                    secrets=granted.secrets,
                    language=understood.language,
                ),
                check,
            )
        except (PlanRejected, FactotumError) as error:
            reason = error.message if isinstance(error, FactotumError) else str(error)
            logger.warning(
                "a plan could not be had", extra={"skill": stored.name, "reason": reason}
            )
            return await self._settle(
                principal,
                understood,
                decision,
                status=ActStatus.FAILED,
                code=ActCode.SKILL_FAILED,
                reply=say(understood.language, Say.FAILED),
                result=ActionResult.FAILED,
                detail=f"no plan: {reason}"[:MAX_FAILURE_DETAIL_CHARS],
                confirmed=confirmed,
                approval=approval,
            )

    async def _give(
        self, principal: Principal, understood: Understood, result: RunResult
    ) -> list[CommandResult]:
        """Have a device do what the skill answered with, after the checks. The device is the one
        the skill was aimed at if it acts on a system device, and the one that was spoken to
        otherwise: a script cannot choose it, and a command the device did not declare it can run is
        not given."""
        if not result.commands:
            return []
        target = understood.target
        device_id: DeviceId | None = (
            target.id if isinstance(target, SystemDeviceRecord) else principal.device_id
        )
        record = (
            None
            if device_id is None
            else await self._directory.system_device(principal.tenant_id, device_id)
        )
        given: list[CommandResult] = []
        for command in result.commands:
            if record is None:
                given.append(
                    CommandResult(
                        status=CommandStatus.UNSUPPORTED, detail="there is no device for it"
                    )
                )
            elif command.requires not in record.capabilities:
                given.append(
                    CommandResult(
                        status=CommandStatus.UNSUPPORTED,
                        detail=f"the device did not declare {command.requires.value!r}",
                    )
                )
            else:
                try:
                    given.append(
                        await self._dispatcher.dispatch(principal.tenant_id, record.id, command)
                    )
                except FactotumError as error:
                    given.append(CommandResult(status=CommandStatus.FAILED, detail=error.message))
        return given

    # ---- what is left behind ------------------------------------------------------------------

    async def _settle(
        self,
        principal: Principal,
        understood: Understood,
        decision: Decision,
        *,
        status: ActStatus,
        code: ActCode | None,
        reply: str,
        result: ActionResult,
        detail: str | None,
        confirmed: bool | None,
        approval: ApprovalTrail | None = None,
    ) -> ActOutcome:
        """Write down what happened, and say it."""
        stored = understood.candidate.skill
        record = await self._record(
            principal,
            skill_id=stored.id,
            skill_name=stored.name,
            target=None if understood.target is None else understood.target.id,
            values=understood.values,
            decision=decision,
            result=result,
            detail=detail,
            confirmed=confirmed,
            approval=approval,
        )
        return ActOutcome(
            status=status,
            reply=_clip(reply, MAX_REPLY_CHARS),
            code=code,
            skill=stored.name,
            reasons=decision.reasons,
            audit_id=record,
        )

    async def _record(
        self,
        principal: Principal,
        *,
        skill_id: SkillId,
        skill_name: str,
        target: DeviceId | None,
        values: dict[str, ParamValue],
        decision: Decision,
        result: ActionResult,
        detail: str | None,
        confirmed: bool | None,
        approval: ApprovalTrail | None = None,
    ) -> AuditId | None:
        """One record of the trail. A trail that cannot be written is said loudly and does not
        change what the person is told: what happened, happened."""
        identity = AuditId(self._new_id())
        try:
            await self._store.add_audit(
                AuditRecord(
                    id=identity,
                    tenant_id=principal.tenant_id,
                    occurred_at=self._now(),
                    device_id=principal.device_id,
                    acts_as=principal.acts_as,
                    skill_id=skill_id,
                    skill_name=skill_name,
                    targets=() if target is None else (target,),
                    params=_redacted(values),
                    decision=decision.outcome,
                    reasons=decision.reasons,
                    confirmed=confirmed,
                    approval=approval,
                    result=result,
                    detail=None if detail is None else _clip(detail, 500),
                )
            )
        except Exception:
            logger.exception(
                "an action could not be audited",
                extra={"skill": skill_name, "result": result.value, "audit": str(identity)},
            )
            return None
        return identity
