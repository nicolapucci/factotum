"""A runner that answers the way a skill would, and does nothing: for development, and for
measuring how the assistant chooses a skill and calls it.

Whether a skill works is one thing, and whether the assistant chooses it and calls it as its
manifest says is another. The second can be measured, and listened to, with skills that do nothing
at all, if what they answer is plausible: a weather that is a weather, an action that is *done*.
A skill with a ``mock.yaml`` beside its ``SKILL.md`` says what it answers, in each language, for
what it was called with; where no runner is set the mock runner reads that file and answers. It
runs no code (the file is data, and the blanks of an answer are filled by name and nothing else),
it does not reach a device, and a skill with no ``mock.yaml`` is answered as the dry run does.

What a mock file looks like::

    replies:
      - when: {room: "*"}            # "*" is any value that was given; anything else is equal to
        it: "In {room} ci sono 21 gradi."
        en: "It is 21 degrees in the {room}."
      - it: "In casa ci sono 21 gradi."   # the last one is for whatever is given
        en: "It is 21 degrees at home."

The first reply whose ``when`` fits is the answer, in the language of the person (English for a
language there are no words for).
"""

import string
from collections.abc import Mapping
from typing import Any, Self

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from factotum.actions.execution import DryRunRunner
from factotum.actions.phrases import language_of
from factotum.actions.store.base import ActionsStore
from factotum.contracts.base import Contract
from factotum.contracts.errors import SkillRunError
from factotum.contracts.runner import RunRequest, RunResult
from factotum.contracts.skills import ParamValue

MOCK_FILE = "mock.yaml"
"""Where a skill says what it answers, beside its ``SKILL.md``."""

MAX_MOCK_BYTES = 20_000
MAX_REPLIES = 30
MAX_REPLY_CHARS = 500

ANY_VALUE = "*"
"""In a ``when``: any value that was given."""

MAX_FIELD_NAME_CHARS = 40


def blanks_of(text: str) -> frozenset[str]:
    """The names of the blanks of an answer (``{room}``), or an error for one that is not a name."""
    names: set[str] = set()
    for _, name, spec, conversion in string.Formatter().parse(text):
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise ValueError(f"a blank of an answer is a name and nothing more: {{{name}}}")
        names.add(name)
    return frozenset(names)


class _Fill(string.Formatter):
    """Fills the blanks of an answer with what the skill was called with, by name: a value that was
    not given is nothing."""

    def get_value(self, key: int | str, args: Any, kwargs: Mapping[str, Any]) -> Any:
        return kwargs.get(str(key), "")


class MockReply(Contract):
    """What a skill answers, for what it was called with."""

    when: dict[str, ParamValue] = Field(default_factory=dict, max_length=10)
    """What the call has to have for this answer: ``"*"`` for a value that was given, or the value
    itself (without regard to case). Nothing is the answer for whatever is given."""

    it: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)
    en: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)

    @field_validator("it", "en")
    @classmethod
    def _blanks_are_names(cls, text: str) -> str:
        blanks_of(text)
        return text

    def fits(self, params: Mapping[str, ParamValue]) -> bool:
        for name, wanted in self.when.items():
            given = params.get(name)
            if given is None or given == "":
                return False
            if wanted != ANY_VALUE and str(given).casefold() != str(wanted).casefold():
                return False
        return True

    def says(self, language: str | None, params: Mapping[str, ParamValue]) -> str:
        text = self.it if language_of(language) == "it" else self.en
        return _Fill().vformat(text, (), dict(params))

    @property
    def blanks(self) -> frozenset[str]:
        return blanks_of(self.it) | blanks_of(self.en)


class MockAnswers(Contract):
    replies: tuple[MockReply, ...] = Field(min_length=1, max_length=MAX_REPLIES)

    @model_validator(mode="after")
    def _the_last_one_is_for_whatever_is_given(self) -> Self:
        if self.replies[-1].when:
            raise ValueError("the last reply is the one for whatever is given: it has no 'when'")
        return self

    def answer(self, params: Mapping[str, ParamValue]) -> MockReply:
        """The first reply that fits what the skill was called with (the last one always does)."""
        return next(reply for reply in self.replies if reply.fits(params))


def read_mock(raw: bytes) -> MockAnswers:
    """What a ``mock.yaml`` says. Raises ``SkillRunError`` for one that is not a mock, by what is
    wrong with it and never by what it holds."""
    if len(raw) > MAX_MOCK_BYTES:
        raise SkillRunError(f"the {MOCK_FILE} of the skill is more than {MAX_MOCK_BYTES} bytes")
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
        if not isinstance(document, dict):
            raise SkillRunError(f"the {MOCK_FILE} of the skill is not a mapping that has replies")
        return MockAnswers.model_validate(document)
    except (UnicodeDecodeError, yaml.YAMLError):
        raise SkillRunError(f"the {MOCK_FILE} of the skill is not YAML") from None
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part)[:MAX_FIELD_NAME_CHARS] for part in problem['loc'])}: "
            f"{problem['msg']}"
            for problem in error.errors(
                include_input=False, include_context=False, include_url=False
            )
        )
        raise SkillRunError(f"the {MOCK_FILE} of the skill is not a mock: {problems}") from None


class MockRunner:
    """Answers a skill from its ``mock.yaml``; one that has none gets the dry run's answer."""

    def __init__(self, store: ActionsStore) -> None:
        self._store = store
        self._dry = DryRunRunner()

    async def run(self, request: RunRequest) -> RunResult:
        files = await self._store.get_files(request.tenant_id, request.skill_id, request.version)
        raw = None if files is None else files.get(MOCK_FILE)
        if raw is None:
            return await self._dry.run(request)
        text = read_mock(raw).answer(request.params).says(request.language, request.params)
        return RunResult(message=text.strip() or None)
