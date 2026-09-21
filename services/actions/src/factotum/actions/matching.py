"""Which skills a message may be asking for.

A model that had to read every skill for every message would make every question slower, so the
skills are first narrowed by meaning: what a skill says about itself (its description and each
example) is embedded, the message is embedded, and only the skills that are near enough go on to
the model that chooses. A message that is far from all of them (most are: they are questions) costs
one embedding and no model call.

The vectors are made with the embedding model of the skill's *owner* and live in its space; when
that model is not there, or has changed, or the skill has not been indexed yet, the skill is not
lost: it goes on to the model unscored, up to a limit, so a skill that was just installed works
before it is indexed and one whose owner has no embedding model works at all.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from factotum.actions.store import ActionsStore, StoredSkill, VectorRow
from factotum.contracts.errors import FactotumError
from factotum.contracts.identity import Owner, Principal, Subject, SubjectKind
from factotum.contracts.ids import SkillId
from factotum.contracts.skills import SkillManifest
from factotum.model_client import Embedder

logger = logging.getLogger("factotum.actions")


@dataclass(frozen=True, slots=True)
class Candidate:
    skill: StoredSkill
    """A skill with a version that runs (``skill.enabled`` is there)."""

    score: float | None = None
    """How near the message is to what the skill says about itself; nothing when it was not
    measured (the skill is not indexed, or its owner has no embedding model)."""

    matched: str | None = None
    """What the skill says that is nearest to the message."""


@dataclass(frozen=True, slots=True)
class Candidates:
    found: tuple[Candidate, ...] = ()
    degraded: bool = False
    """Some skills were not measured because an embedding model could not be reached."""


def what_a_skill_says_about_itself(manifest: SkillManifest) -> list[str]:
    """The texts that are embedded to find a skill: what it is for, and how it is asked for."""
    return [manifest.description, *manifest.examples]


def owners_of(principal: Principal) -> list[Owner]:
    """Whose skills a caller may use: the tenant's, and their own when they act as a person."""
    owners = [Owner.of_subject(Subject.tenant(principal.tenant_id))]
    if principal.acts_as.kind is SubjectKind.USER:
        owners.append(Owner.of_subject(principal.acts_as))
    return owners


@dataclass
class SkillIndex:
    """Making the vectors of what skills say about themselves, in the space of their owner's
    embedding model."""

    store: ActionsStore
    embedder: Embedder
    timeout_s: float = 8.0
    _tasks: set["asyncio.Task[None]"] = field(default_factory=set)
    _underway: set[SkillId] = field(default_factory=set)

    async def index(self, skill: StoredSkill) -> bool:
        """Index the version of a skill that runs. False when it could not be done (no embedding
        model, the gateway away): the skill is then found unscored until it can."""
        if skill.enabled is None:
            return False
        texts = what_a_skill_says_about_itself(skill.enabled.manifest)
        try:
            async with asyncio.timeout(self.timeout_s):
                embedded = await self.embedder.embed(skill.owner, texts)
        except (FactotumError, TimeoutError) as error:
            logger.warning(
                "a skill could not be indexed",
                extra={"skill": skill.name, "reason": type(error).__name__},
            )
            return False
        await self.store.forget_other_spaces(skill.tenant_id, skill.owner, embedded.space_id)
        await self.store.put_vectors(
            skill.tenant_id,
            skill.id,
            skill.enabled.meta.version,
            embedded.space_id,
            [VectorRow(text, vector) for text, vector in zip(texts, embedded.vectors, strict=True)],
        )
        return True

    def later(self, skills: list[StoredSkill]) -> None:
        """Index skills in the background, without keeping anybody waiting for it. A skill that is
        being indexed is not started on again by the next message."""
        wanted = [skill for skill in skills if skill.id not in self._underway]
        if not wanted:
            return
        self._underway.update(skill.id for skill in wanted)
        task = asyncio.create_task(self._all(wanted), name="index-skills")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _all(self, skills: list[StoredSkill]) -> None:
        for skill in skills:
            try:
                await self.index(skill)
            finally:
                self._underway.discard(skill.id)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class SkillMatcher:
    def __init__(
        self,
        store: ActionsStore,
        embedder: Embedder,
        index: SkillIndex,
        *,
        threshold: float = 0.45,
        max_candidates: int = 6,
        max_unscored: int = 12,
        timeout_s: float = 4.0,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._index = index
        self._threshold = threshold
        self._max_candidates = max_candidates
        self._max_unscored = max_unscored
        self._timeout_s = timeout_s

    async def candidates(self, principal: Principal, message: str) -> Candidates:
        """The skills of the caller that the message may be asking for, the nearest first, then the
        ones that could not be measured."""
        tenant = principal.tenant_id
        skills = await self._store.list_skills(tenant, owners_of(principal), enabled_only=True)
        if not skills:
            return Candidates()

        scored: list[Candidate] = []
        unscored: list[Candidate] = []
        degraded = False
        for owner in {skill.owner for skill in skills}:
            mine = [skill for skill in skills if skill.owner == owner]
            try:
                async with asyncio.timeout(self._timeout_s):
                    embedded = await self._embedder.embed(owner, [message])
            except (FactotumError, TimeoutError) as error:
                logger.warning(
                    "a message could not be embedded", extra={"reason": type(error).__name__}
                )
                degraded = True
                unscored.extend(Candidate(skill) for skill in mine)
                continue

            indexed = await self._store.indexed(tenant, owner, embedded.space_id)
            found = await self._store.search_vectors(
                tenant, owner, embedded.space_id, embedded.vectors[0], limit=self._max_candidates
            )
            by_id: dict[SkillId, StoredSkill] = {skill.id: skill for skill in mine}
            scored.extend(
                Candidate(by_id[match.skill_id], match.score, match.matched)
                for match in found
                if match.score >= self._threshold and match.skill_id in by_id
            )
            missing = [skill for skill in mine if skill.id not in indexed]
            unscored.extend(Candidate(skill) for skill in missing)
            self._index.later(missing)

        scored.sort(key=lambda c: c.score or 0.0, reverse=True)
        unscored.sort(key=lambda c: c.skill.name)
        chosen = [*scored[: self._max_candidates], *unscored[: self._max_unscored]]
        return Candidates(tuple(chosen), degraded)
