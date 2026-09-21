"""Stand-ins for what the actions service depends on (a chat model, an embedding model, a runner,
the devices that commands go to, the clock), so that the tests of everything above them need no
network and no model."""

import asyncio
import re
import zlib
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from math import sqrt
from typing import Any
from uuid import uuid4

from factotum.actions.store import StoredSkill, StoredVersion, VersionMeta
from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    EventUnion,
    FinishReason,
)
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import SkillId, TenantId
from factotum.contracts.models import ModelRole, Profile, StructuredOutputSupport
from factotum.contracts.skills import (
    ApprovalPolicy,
    ConfirmPolicy,
    ParameterSpec,
    ParamType,
    SkillManifest,
    SkillPermissions,
    SkillTarget,
)
from factotum.model_client import Embedded

START = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


class Clock:
    """A clock that moves when it is told to, and by a ``tick`` each time it is read when it is
    told to do that: what is written one after another is then written in that order."""

    def __init__(self, now: datetime = START, *, tick: timedelta = timedelta(0)) -> None:
        self.current = now
        self._tick = tick

    def __call__(self) -> datetime:
        now = self.current
        self.current += self._tick
        return now

    def advance(self, **how_much: float) -> datetime:
        self.current += timedelta(**how_much)
        return self.current


class ScriptedChat:
    """A chat model that answers as told and keeps what it was asked.

    ``answers`` are what it says to the next requests for a JSON answer (a dict, or an error to
    raise), in order. A request nobody scripted is a failure of the test, not a made-up answer.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[Owner, ChatRequest]] = []
        self.answers: list[dict[str, Any] | Exception] = []

    def say(self, *answers: dict[str, Any] | Exception) -> None:
        self.answers.extend(answers)

    async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
        self.requests.append((owner, request))
        if not self.answers:
            raise AssertionError("the model was asked something that the test did not expect")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ChatResult(text=str(answer), parsed=answer, finish_reason=FinishReason.STOP)

    def chat_stream(self, owner: Owner, request: ChatRequest) -> AsyncGenerator[EventUnion]:
        raise NotImplementedError("the actions service asks for whole answers")

    async def profile(self, owner: Owner, role: ModelRole) -> Profile:
        return Profile(
            role=role,
            max_input_tokens=8192,
            max_output_tokens=1024,
            streaming=True,
            structured_output=StructuredOutputSupport.NATIVE,
        )

    @property
    def asked(self) -> list[ChatRequest]:
        return [request for _, request in self.requests]


_WORD = re.compile(r"[a-z0-9]+")
DIMENSIONS = 64


def embed_words(text: str) -> tuple[float, ...]:
    """A vector for a text, from its words: texts that share words are near, and texts that share
    none are not. Deterministic, and nothing like a real model, which is the point: what is tested
    is what the service does with the distances."""
    vector = [0.0] * DIMENSIONS
    for word in _WORD.findall(text.casefold()):
        vector[zlib.crc32(word.encode()) % DIMENSIONS] += 1.0
    norm = sqrt(sum(x * x for x in vector))
    return tuple(x / norm for x in vector) if norm else tuple(vector)


class FakeEmbedder:
    """An embedding model that is made of words, and can be told to fail or to change."""

    def __init__(self, space_id: str = "space-a") -> None:
        self.space_id = space_id
        self.calls: list[tuple[Owner, tuple[str, ...]]] = []
        self.failure: Exception | None = None
        self.delay_s = 0.0
        """How long it takes to answer: for a test of what is done when it takes too long."""

    async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
        self.calls.append((owner, tuple(texts)))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.failure is not None:
            raise self.failure
        return Embedded(
            vectors=tuple(embed_words(text) for text in texts),
            space_id=self.space_id,
            dimensions=DIMENSIONS,
        )


# ---- skills, as they are stored, without going through a file

LAMP = SkillManifest(
    name="lamp",
    description="Turn a lamp on or off, or set how bright it is.",
    examples=("turn on the living room lamp", "switch off the lamp", "accendi la luce"),
    target=SkillTarget.EXTERNAL,
    parameters={
        "state": ParameterSpec(type=ParamType.STRING, enum=("on", "off")),
        "brightness": ParameterSpec(type=ParamType.INTEGER, minimum=0, maximum=100),
    },
    required=("state",),
    device_parameters={
        "entity_id": ParameterSpec(type=ParamType.STRING),
        "host": ParameterSpec(type=ParamType.STRING),
    },
    permissions=SkillPermissions(network=("${device.host}",), secrets=("HA_TOKEN",)),
    effect=Effect.REVERSIBLE,
    entrypoint="scripts/run.py",
)
GARAGE = SkillManifest(
    name="garage-door",
    description="Open or close the garage door.",
    examples=("open the garage door", "close the garage", "apri il garage"),
    target=SkillTarget.EXTERNAL,
    parameters={"action": ParameterSpec(type=ParamType.STRING, enum=("open", "close"))},
    required=("action",),
    device_parameters={
        "entity_id": ParameterSpec(type=ParamType.STRING),
        "host": ParameterSpec(type=ParamType.STRING),
    },
    permissions=SkillPermissions(network=("${device.host}",), secrets=("HA_TOKEN",)),
    effect=Effect.REVERSIBLE,
    confirm=ConfirmPolicy.ASK,
    entrypoint="scripts/run.py",
)
VOLUME = SkillManifest(
    name="volume",
    description="Set the volume of the device that is being spoken to.",
    examples=("set the volume to 30", "volume at fifty percent"),
    target=SkillTarget.SYSTEM,
    parameters={"level": ParameterSpec(type=ParamType.INTEGER, minimum=0, maximum=100)},
    required=("level",),
    effect=Effect.REVERSIBLE,
    entrypoint="scripts/run.py",
)
WEATHER = SkillManifest(
    name="weather",
    description="Say what the weather is like now in a city.",
    examples=("what is the weather like in Rome", "is it raining in Milan"),
    parameters={"place": ParameterSpec(type=ParamType.STRING)},
    required=("place",),
    permissions=SkillPermissions(network=("api.open-meteo.com",)),
    effect=Effect.READ_ONLY,
    entrypoint="scripts/run.py",
)
ROUTER = SkillManifest(
    name="restart-router",
    description="Restart the router of the home: everybody is disconnected for a while.",
    examples=("restart the router", "reboot the wifi"),
    permissions=SkillPermissions(network=("router.local",)),
    effect=Effect.REVERSIBLE,
    approval=ApprovalPolicy.REQUIRED,
    entrypoint="scripts/run.py",
)


def stored_skill(
    manifest: SkillManifest,
    tenant: TenantId,
    *,
    owner: Owner | None = None,
    enabled: bool = True,
    version: int = 1,
    skill_id: SkillId | None = None,
) -> StoredSkill:
    """A skill as the store hands it out, owned by the tenant unless an owner is given, with its
    version enabled (with what it declares granted) unless it is not to be."""
    meta = VersionMeta(
        version=version,
        content_sha256=f"{version:064x}",
        created_at=START,
        enabled_at=START if enabled else None,
    )
    held = StoredVersion(
        meta=meta,
        manifest=manifest,
        instructions="",
        granted=manifest.permissions if enabled else None,
    )
    return StoredSkill(
        id=skill_id or SkillId(uuid4()),
        tenant_id=tenant,
        owner=owner or Owner.of_subject(Subject.tenant(tenant)),
        name=manifest.name,
        created_at=START,
        versions=(meta,),
        latest=held,
        enabled=held if enabled else None,
    )
