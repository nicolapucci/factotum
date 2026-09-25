"""The plan that a model writes for a skill in instructions mode.

A skill that has no script is a markdown document: what to do, in words. A model reads it, and the
values a person asked for, and writes a *plan*: data, not code. The platform checks the plan against
what the skill was granted (``factotum.actions.instructions``), and then runs it in the sandbox with
a program of its own, which does what the plan says and nothing else. The model never runs anything
and the plan holds no secret: it holds the *name* of one, which the platform puts in when it is run.

A plan is a few steps, in order. A step is a request (``http``) or something a device is to do
(``command``), and it may say where in the answer of an earlier request a value is to be found:

* ``${secret.NAME}``: a secret that the skill was granted;
* ``${device.NAME}``: a value of the device the skill is bound to (its host, its entity);
* ``${value.NAME}``: a value that the person's message gave;
* ``${step.ID.status}``, ``${step.ID.text}``, ``${step.ID.json.PATH}``: the status, the text, or a
  field of the JSON of an earlier request (a path is keys and list positions with dots between them,
  ``results.0.latitude``), each optionally followed by ``|round``.

This module is what a plan is. What a placeholder may refer to, and where, is decided where the plan
is checked, and what it comes to is decided where it is run.
"""

import json
import re
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from factotum.contracts.base import Contract

MAX_STEPS = 8
MAX_URL_CHARS = 2000
MAX_HEADERS = 10
MAX_HEADER_CHARS = 1000
MAX_BODY_CHARS = 16_000
"""The most that a body is, as text, before the placeholders are put in."""

MAX_SAY_CHARS = 600
"""The most that the sentence to say is, as a template."""

MAX_PLAN_CHARS = 40_000
"""The most that a plan is, written out: what goes to the sandbox to be run has to fit on one line
of input, with what the person asked for."""

_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class PlanStep(Contract):
    """One step: a request, or something a device is to do. The fields of the other kind are left
    empty (a model that writes ``null`` for them is right)."""

    id: str
    """A short name, for what comes after to refer to its result by."""

    kind: Literal["http", "command"]

    # ---- a request
    method: HttpMethod | None = None
    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_HEADERS)
    body: Any = None
    """A JSON value is sent as JSON, and a text as it is."""

    # ---- a command
    name: str | None = Field(default=None, max_length=40)
    args: dict[str, Any] = Field(default_factory=dict, max_length=10)

    @field_validator("id")
    @classmethod
    def _is_a_short_name(cls, name: str) -> str:
        if not _ID.fullmatch(name):
            raise ValueError("a step is called by a short name in lower case: letters, digits, _")
        return name

    @field_validator("headers")
    @classmethod
    def _headers_are_short(cls, headers: dict[str, str]) -> dict[str, str]:
        if any(len(name) > 100 or len(value) > MAX_HEADER_CHARS for name, value in headers.items()):
            raise ValueError("a header is short")
        return headers

    @field_validator("body")
    @classmethod
    def _a_body_is_not_a_book(cls, body: Any) -> Any:
        try:
            size = len(body) if isinstance(body, str) else len(json.dumps(body))
        except (TypeError, ValueError):
            raise ValueError("a body is JSON, or text") from None
        if size > MAX_BODY_CHARS:
            raise ValueError(f"a body is at most {MAX_BODY_CHARS} characters")
        return body

    @model_validator(mode="after")
    def _has_what_its_kind_needs_and_nothing_of_the_other(self) -> Self:
        if self.kind == "http":
            if self.method is None or not self.url:
                raise ValueError("a request has a method and an address")
            if self.name is not None or self.args:
                raise ValueError("a request has no command")
        else:
            if not self.name:
                raise ValueError("a command has a name")
            if self.method is not None or self.url or self.headers or self.body is not None:
                raise ValueError("a command has no request")
        return self


class InstructionPlan(Contract):
    reason: str = Field(max_length=500)
    """What it does, in a few words: the answer that comes first is the one a small model gets
    right, and it is what the trail says of a plan."""

    steps: tuple[PlanStep, ...] = Field(default=(), max_length=MAX_STEPS)
    """What is done, in this order. None is a plan that could not be made."""

    say: str | None = Field(default=None, max_length=MAX_SAY_CHARS)
    """What to say when it is all done, as a template."""

    @model_validator(mode="after")
    def _steps_are_called_differently(self) -> Self:
        names = [step.id for step in self.steps]
        if len(set(names)) != len(names):
            raise ValueError("every step has a name of its own")
        return self

    @model_validator(mode="after")
    def _a_plan_is_not_a_book(self) -> Self:
        if len(self.model_dump_json()) > MAX_PLAN_CHARS:
            raise ValueError(f"a plan is at most {MAX_PLAN_CHARS} characters")
        return self
