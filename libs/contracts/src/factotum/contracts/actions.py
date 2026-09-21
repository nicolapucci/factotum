"""The action path: what is asked of the actions service, and what it answers.

A message that asks for something to be done ("turn on the living room lamp") is not a question
for the documents. The ``actions`` service matches it with a skill, authorizes it (``authorize``),
asks for a confirmation or an approval when the decision says so, runs it and audits it. The
conversation service asks it, before it searches the documents, whether a message is one of those:
``ActOutcome.status`` says whether it was (``NO_ACTION``: it was not, answer it as a question) and,
if it was, how it went, with the words to say.

This module also has the catalog of skills as an administrator sees it: what is installed, in which
versions, and which version was reviewed and enabled.
"""

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from factotum.contracts.audit import AuditRecord
from factotum.contracts.authorization import Reason
from factotum.contracts.base import Contract
from factotum.contracts.conversation import MAX_MESSAGE_CHARS
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner
from factotum.contracts.ids import AuditId, SkillId, TenantId, UserId
from factotum.contracts.skills import SkillManifest, SkillMode, SkillPermissions, SkillTarget

MAX_REPLY_CHARS = 2000
"""The most that is said in one piece: what a device speaks in one part."""

MAX_SKILL_SOURCE_CHARS = 100_000
"""The largest ``SKILL.md`` that is installed."""


class ActStatus(StrEnum):
    """What became of a message that may have been a request to do something."""

    NO_ACTION = "no_action"
    """It asks for none of the skills: answer it as a question."""

    DONE = "done"
    """A skill was run (or, while there is no runner, pretended to be)."""

    NEEDS_CONFIRMATION = "needs_confirmation"
    """A skill was chosen and a plain yes or no is asked before it runs; the reply is the question.
    The next message on the same device answers it."""

    NEEDS_MORE = "needs_more"
    """A skill was chosen but a value or the device it is for is missing, or ambiguous: the reply
    asks, and the person says the whole request again."""

    CANCELLED = "cancelled"
    """The person said no."""

    REFUSED = "refused"
    """Not allowed, or not approved. The reply says so, and ``code`` and ``reasons`` say why."""

    FAILED = "failed"
    """It was attempted and did not work."""


class ActCode(StrEnum):
    """Why an action came out the way it did, when the status alone does not say."""

    # NEEDS_MORE
    MISSING_VALUES = "missing_values"
    AMBIGUOUS_DEVICE = "ambiguous_device"
    NO_DEVICE = "no_device"
    """The skill acts on a device and none that the caller may use is set up for it."""

    # CANCELLED
    DECLINED = "declined"

    # REFUSED
    DENIED = "denied"
    """The policy says no; ``reasons`` say which rule."""

    APPROVAL_UNAVAILABLE = "approval_unavailable"
    """It needs the approval of an authorized person and there is no way to ask for one."""

    APPROVAL_DENIED = "approval_denied"
    APPROVAL_EXPIRED = "approval_expired"
    UNKNOWN_DEVICE = "unknown_device"
    """The request came from a device that is not known: nothing is done for it."""

    SKILL_NOT_ENABLED = "skill_not_enabled"
    """The skill was disabled after it was chosen, before it was confirmed."""

    # FAILED
    SKILL_FAILED = "skill_failed"
    RUNNER_UNAVAILABLE = "runner_unavailable"
    COMMAND_FAILED = "command_failed"
    """The skill ran, and a command it returned for a device did not work."""


class ActRequest(Contract):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    untrusted_content_in_turn: bool = False
    """True when text from the web or from a document entered the turn: nothing that has an effect
    then runs without a confirmation (see ``authorize``)."""


class ActOutcome(Contract):
    status: ActStatus
    reply: str | None = Field(default=None, min_length=1, max_length=MAX_REPLY_CHARS)
    """What to say, in the language of the message. Only ``NO_ACTION`` has none."""

    code: ActCode | None = None
    skill: str | None = Field(default=None, max_length=64)
    """The name of the skill that was chosen, if one was."""

    reasons: tuple[Reason, ...] = ()
    audit_id: AuditId | None = None
    """The record this left in the audit trail, if it left one."""

    @model_validator(mode="after")
    def _says_something_unless_there_was_nothing_to_do(self) -> Self:
        if self.status is ActStatus.NO_ACTION:
            if self.reply is not None or self.code is not None or self.skill is not None:
                raise ValueError("a message that is no action has no reply, code or skill")
        elif self.reply is None:
            raise ValueError("an action has something to say")
        return self

    @classmethod
    def no_action(cls) -> Self:
        return cls(status=ActStatus.NO_ACTION)

    @property
    def handled(self) -> bool:
        """Whether the message was an action: the reply is the answer to the turn."""
        return self.status is not ActStatus.NO_ACTION


# ---- the catalog of skills ---------------------------------------------------------------------


class SkillOwner(StrEnum):
    """Who a skill that is installed belongs to."""

    USER = "user"
    TENANT = "tenant"


class SkillInstall(Contract):
    """A skill to install: the text of its ``SKILL.md``."""

    source: str = Field(min_length=1, max_length=MAX_SKILL_SOURCE_CHARS)
    owner: SkillOwner = SkillOwner.TENANT


class SkillEnable(Contract):
    """Review and enable a version: the permissions it declares are granted as they are. Without a
    ``version``, the latest one."""

    version: int | None = Field(default=None, ge=1)


class SkillVersionInfo(Contract):
    version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    enabled: bool
    """Whether this is the version that runs."""

    enabled_at: AwareDatetime | None = None
    enabled_by: UserId | None = None


class SkillInfo(Contract):
    id: SkillId
    tenant_id: TenantId
    owner: Owner
    name: str
    description: str
    target: SkillTarget
    mode: SkillMode
    effect: Effect
    latest_version: int = Field(ge=1)
    enabled_version: int | None = Field(default=None, ge=1)
    """The version that runs. None: nothing runs until an administrator enables one."""


class SkillDetail(SkillInfo):
    manifest: SkillManifest
    """The latest version's."""

    instructions: str
    versions: tuple[SkillVersionInfo, ...] = ()
    granted: SkillPermissions | None = None
    """The permissions granted to the enabled version, when there is one."""


class SkillList(Contract):
    skills: tuple[SkillInfo, ...] = ()


class SkillLoadProblem(Contract):
    """A skill that could not be read, and where it was."""

    where: str = Field(max_length=300)
    message: str = Field(max_length=500)


class SkillReload(Contract):
    """What reading the folder of skills again came to."""

    installed: tuple[str, ...] = ()
    """``name@version`` of what was new."""

    enabled: tuple[str, ...] = ()
    """``name@version`` of what was enabled by reading it (development only: where a folder is the
    administrator's own, what is in it is taken to be reviewed)."""

    unchanged: int = Field(default=0, ge=0)
    problems: tuple[SkillLoadProblem, ...] = ()


class AuditList(Contract):
    records: tuple[AuditRecord, ...] = ()
    next_before: AwareDatetime | None = None
    """Ask for the ones before this to go on."""
