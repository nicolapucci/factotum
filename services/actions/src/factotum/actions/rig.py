"""The action service with everything around it made of doubles, in a home of devices: what the
tests of the service, of its HTTP interface and of its wiring are built on."""

import asyncio
from datetime import timedelta
from typing import Any

from factotum.actions.confirmations import ConfirmationReader
from factotum.actions.directory import DeviceDirectory, FileDeviceDirectory
from factotum.actions.execution import CommandDispatcher, SkillRunner
from factotum.actions.matching import SkillIndex, SkillMatcher
from factotum.actions.planner import Planner
from factotum.actions.service import ActionService, ActionSettings
from factotum.actions.skills import LoadedSkill
from factotum.actions.store import ActionsStore, InMemoryStore, StoredSkill
from factotum.actions.testing import Clock, FakeEmbedder, ScriptedChat
from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.actions import ActOutcome, ActRequest
from factotum.contracts.approval import ApprovalRequest, ApprovalResult
from factotum.contracts.audit import AuditRecord
from factotum.contracts.commands import CommandResult, DeviceCommand
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.identity import Owner, Principal, Role, Subject
from factotum.contracts.ids import DeviceId, TenantId, UserId
from factotum.contracts.runner import RunRequest, RunResult
from factotum.contracts.skills import SkillManifest
from factotum.contracts.testing import make_tenant_id, make_user_id
from factotum.devices.home import Home, parse_home
from factotum.model_client import Chat, Embedder


class FakeRunner:
    """A runner that answers as told and keeps what it was asked. ``results`` are by skill name (a
    result to give, or an error to raise); without one it answers ``default``."""

    def __init__(self) -> None:
        self.requests: list[RunRequest] = []
        self.results: dict[str, RunResult | Exception] = {}
        self.default = RunResult(message="Done by the runner.")
        self.delay_s = 0.0

    async def run(self, request: RunRequest) -> RunResult:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        result = self.results.get(request.skill_name, self.default)
        if isinstance(result, Exception):
            raise result
        return result


class FakeDispatcher:
    """A dispatcher that keeps the commands it was given, and answers OK unless told otherwise."""

    def __init__(self) -> None:
        self.given: list[tuple[TenantId, DeviceId, DeviceCommand]] = []
        self.answer: CommandResult | Exception = CommandResult.ok()

    async def dispatch(
        self, tenant_id: TenantId, device_id: DeviceId, command: DeviceCommand
    ) -> CommandResult:
        self.given.append((tenant_id, device_id, command))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeApprover:
    """An approver that answers as told and keeps what it was asked."""

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []
        self.answer: ApprovalResult | Exception = ApprovalResult.unavailable()
        self.delay_s = 0.0

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


TEST_SETTINGS = ServiceSettings(_env_file=None, environment=Environment.TEST)

HOME_TEXT = """
devices:
  - name: Kitchen speaker
    hardware_id: kitchen-01
    token: kitchen-credential
    owner: tenant
    capabilities: [audio_in, audio_out, led]
  - name: Living room display
    hardware_id: living-room-01
    token: living-room-credential
    owner: tenant
    capabilities: [audio_in, audio_out, display, led]
    policy: {max_effect: irreversible}
  - name: Study player
    hardware_id: study-01
    token: study-credential
    owner: tenant
    capabilities: [audio_out, media_player]
    aliases: [the player]
  - name: My phone
    hardware_id: phone-01
    token: phone-credential
    owner: user
    capabilities: [audio_in, audio_out, display]
  - kind: external
    name: Living room lamp
    aliases: [lamp, the lamp]
  - kind: external
    name: Bedroom lamp
    owner: user
    aliases: [my lamp]
  - kind: external
    name: Garage door
    aliases: [garage]
    sensitivity: irreversible
bindings:
  - skill: lamp
    device: Living room lamp
    params: {entity_id: light.living_room, host: homeassistant.local}
  - skill: lamp
    device: Bedroom lamp
    params: {entity_id: light.bedroom, host: homeassistant.local}
  - skill: garage-door
    device: Garage door
    params: {entity_id: cover.garage, host: homeassistant.local}
"""


def principal_of_device(device: SystemDeviceRecord) -> Principal:
    """Who a device acts for: a personal one for its owner, a shared one for the tenant."""
    if device.owner.kind.value == "user":
        return Principal(
            tenant_id=device.tenant_id,
            device_id=device.id,
            acts_as=Subject.user(UserId(device.owner.id)),
            roles=frozenset({Role.MEMBER}),
        )
    return Principal(
        tenant_id=device.tenant_id, device_id=device.id, acts_as=Subject.tenant(device.tenant_id)
    )


def plan_of(skill: str | None = None, **fields: Any) -> dict[str, Any]:
    """What a model says for a message: the plan, with the fields it leaves out left out."""
    return {
        "reason": "as told",
        "skill": skill,
        "device": None,
        "parameters": {},
        "language": "en",
        "question": None,
        "ask": None,
        **fields,
    }


class ActionsRig:
    def __init__(
        self,
        *,
        home_text: str = HOME_TEXT,
        settings: ActionSettings | None = None,
        runner: SkillRunner | None = None,
        store: ActionsStore | None = None,
        chat: Chat | None = None,
        embedder: Embedder | None = None,
        patience_s: float | None = None,
    ) -> None:
        """``chat`` and ``embedder`` are real models for a test that measures them; without them
        the doubles are used, and ``plans`` and ``embedder`` are how they are told what to do.
        ``patience_s`` is how long each model is waited for, where the defaults would give up on a
        model that is only slow, which is not what such a test measures."""
        self.tenant = make_tenant_id()
        self.user = make_user_id()
        self.home: Home = parse_home(home_text, tenant_id=self.tenant, user_id=self.user)
        self.directory: DeviceDirectory = FileDeviceDirectory(TEST_SETTINGS, self.home)
        self.store: ActionsStore = store or InMemoryStore()
        self.chat = ScriptedChat()
        self.embedder = FakeEmbedder()
        self._chat: Chat = chat or self.chat
        self._embedder: Embedder = embedder or self.embedder
        self._patience_s = patience_s
        self.index = SkillIndex(self.store, self._embedder, timeout_s=self._wait(8.0))
        self.matcher = SkillMatcher(
            self.store, self._embedder, self.index, timeout_s=self._wait(4.0)
        )
        self.runner = FakeRunner()
        self._runner: SkillRunner = runner or self.runner
        self.dispatcher = FakeDispatcher()
        self._dispatcher: CommandDispatcher = self.dispatcher
        self.approver = FakeApprover()
        self.clock = Clock(tick=timedelta(milliseconds=1))
        self._settings = settings
        self.service = self._service()

    def _wait(self, default_s: float) -> float:
        return default_s if self._patience_s is None else self._patience_s

    def _service(self) -> ActionService:
        return ActionService(
            store=self.store,
            directory=self.directory,
            matcher=self.matcher,
            planner=Planner(self._chat, timeout_s=self._wait(10.0)),
            reader=ConfirmationReader(self._chat, timeout_s=self._wait(6.0)),
            runner=self._runner,
            dispatcher=self._dispatcher,
            approver=self.approver,
            settings=self._settings,
            now=self.clock,
        )

    def use_directory(self, directory: DeviceDirectory) -> None:
        """Go on with the same store, models and clock but another knowledge of the devices: the
        home has changed since something was asked."""
        self.directory = directory
        self.service = self._service()

    def use_runner(self, runner: SkillRunner, dispatcher: CommandDispatcher | None = None) -> None:
        """Go on with the same store, models and clock but another runner (and, when one is given,
        another way to reach the devices): the ones that run skills for real, in place of the
        doubles."""
        self._runner = runner
        if dispatcher is not None:
            self._dispatcher = dispatcher
        self.service = self._service()

    def device(self, name: str) -> SystemDeviceRecord:
        return next(record for record, _ in self.home.system if record.name == name)

    def principal(self, device: str | None) -> Principal:
        """The principal of a device of the home, or of an administrator in a hub session (with no
        device) for ``None``."""
        if device is None:
            return Principal(
                tenant_id=self.tenant,
                acts_as=Subject.user(self.user),
                roles=frozenset({Role.ADMIN}),
            )
        return principal_of_device(self.device(device))

    @property
    def shared_owner(self) -> Owner:
        return Owner.of_subject(Subject.tenant(self.tenant))

    async def install(
        self,
        *manifests: SkillManifest,
        enable: bool = True,
        index: bool = True,
        owner: Owner | None = None,
    ) -> dict[str, StoredSkill]:
        """Skills that are installed, enabled with what they declare, and indexed."""
        found: dict[str, StoredSkill] = {}
        for manifest in manifests:
            installed = await self.store.install(
                self.tenant,
                owner or self.shared_owner,
                LoadedSkill(
                    manifest,
                    "",
                    f"---\n{manifest.model_dump_json()}\n---\n",
                    {"scripts/run.py": b"x"},
                ),
                now=self.clock(),
            )
            if enable:
                await self.store.enable(
                    self.tenant,
                    installed.skill_id,
                    installed.version,
                    granted=manifest.permissions,
                    by=self.user,
                    now=self.clock(),
                )
            stored = await self.store.get_skill(self.tenant, installed.skill_id)
            if stored is None:
                raise AssertionError("a skill that was installed is not there")
            if enable and index and not await self.index.index(stored):
                raise AssertionError("a skill that runs could not be indexed")
            found[manifest.name] = stored
        return found

    def plans(self, *plans: dict[str, Any] | Exception) -> None:
        """What the model will say to the next messages it is asked to plan (or to read)."""
        self.chat.say(*plans)

    async def hear(
        self, device: str | None, message: str, *, untrusted: bool = False
    ) -> ActOutcome:
        return await self.service.act(
            self.principal(device),
            ActRequest(message=message, untrusted_content_in_turn=untrusted),
        )

    async def audit(self) -> list[AuditRecord]:
        """The trail, the oldest first."""
        records = await self.store.list_audit(self.tenant, before=None, limit=100)
        return sorted(records, key=lambda record: record.occurred_at)
