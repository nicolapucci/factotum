"""Structured output: a JSON value that matches a schema, from any chat model.

Some providers can constrain the output to a schema themselves. For the others, and for servers
whose support is uncertain, the gateway asks for JSON in the prompt, validates the answer and
retries with the errors it found. Either way the caller receives a value that has been validated
against the schema, never just text that looks like JSON.
"""

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from factotum.contracts.chat import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    FinishReason,
    MessageRole,
    Usage,
)
from factotum.contracts.errors import (
    ContentFilteredError,
    InvalidSchemaError,
    InvalidStructuredOutputError,
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)

# What OpenAI's strict mode accepts, kept conservative: a schema that uses anything else is sent
# without `strict` and validated here instead.
_STRICT_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "description",
        "anyOf",
        "title",
    }
)
_MAX_REPORTED_ERRORS = 5

ChatCall = Callable[[ChatRequest], Awaitable[ChatResult]]


def validator_for(schema: dict[str, Any]) -> Draft202012Validator:
    """A validator for ``schema``, or ``InvalidSchemaError`` when it is not a valid JSON Schema."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise InvalidSchemaError(
            f"the response schema is not a valid JSON Schema: {error.message}"
        ) from None
    return Draft202012Validator(schema)


def strict_compatible(schema: Any) -> bool:
    """Whether ``schema`` fits a provider's strict structured-output mode: every object forbids
    extra properties and lists all its properties as required, using only basic keywords."""
    if not isinstance(schema, dict):
        return False
    if not set(schema) <= _STRICT_KEYWORDS:
        return False
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            return False
        if set(schema.get("required", [])) != set(properties):
            return False
        if not all(strict_compatible(sub) for sub in properties.values()):
            return False
    if "items" in schema and not strict_compatible(schema["items"]):
        return False
    return all(strict_compatible(sub) for sub in schema.get("anyOf", []))


_SCHEMA_MAPS = frozenset({"properties", "$defs", "patternProperties"})
_SCHEMA_LISTS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SCHEMA_VALUES = frozenset({"items", "additionalProperties", "not", "if", "then", "else"})


def keywords_within(schema: Any, allowed: frozenset[str]) -> bool:
    """Whether ``schema``, at every depth, uses only the keywords in ``allowed``.

    For a provider that enforces a documented subset of JSON Schema: a schema outside it is better
    asked for in the prompt and checked here than sent to be refused.
    """
    if isinstance(schema, bool):
        return True
    if not isinstance(schema, dict) or not set(schema) <= allowed:
        return False
    for key, value in schema.items():
        if key in _SCHEMA_MAPS:
            inside = isinstance(value, dict) and all(
                keywords_within(sub, allowed) for sub in value.values()
            )
        elif key in _SCHEMA_LISTS:
            inside = isinstance(value, list) and all(keywords_within(sub, allowed) for sub in value)
        elif key in _SCHEMA_VALUES:
            inside = keywords_within(value, allowed)
        else:
            inside = True
        if not inside:
            return False
    return True


def extract_json(text: str) -> Any:
    """The JSON value in a model's answer, tolerating a code fence or a sentence around it.

    Raises ``ValueError`` when there is none.
    """
    candidate = text.strip()
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char in "{[":
            try:
                value, _end = decoder.raw_decode(candidate, index)
            except ValueError:
                continue
            return value
    raise ValueError("no JSON value found in the answer")


def json_instructions(schema: dict[str, Any]) -> str:
    return (
        "Reply with a single JSON value that validates against the JSON Schema below. "
        "Output only the JSON: no prose and no code fences.\n\n"
        f"JSON Schema:\n{json.dumps(schema, indent=2)}"
    )


def with_json_instructions(
    messages: Sequence[ChatMessage], schema: dict[str, Any]
) -> tuple[ChatMessage, ...]:
    """The conversation with the JSON instructions added to the system message."""
    instructions = json_instructions(schema)
    first, *rest = messages
    if first.role is MessageRole.SYSTEM:
        merged = ChatMessage(role=MessageRole.SYSTEM, content=f"{first.content}\n\n{instructions}")
        return (merged, *rest)
    return (ChatMessage(role=MessageRole.SYSTEM, content=instructions), *messages)


def repair_messages(
    messages: Sequence[ChatMessage], answer: str, problems: Sequence[str]
) -> tuple[ChatMessage, ...]:
    """The conversation extended with the invalid answer and what was wrong with it."""
    listing = "\n".join(f"- {problem}" for problem in problems)
    correction = (
        f"Your previous answer did not match the schema:\n{listing}\n"
        "Reply again with only the corrected JSON."
    )
    return (
        *messages,
        ChatMessage(role=MessageRole.ASSISTANT, content=answer or "(empty)"),
        ChatMessage(role=MessageRole.USER, content=correction),
    )


def _problems(validator: Draft202012Validator, value: Any) -> list[str]:
    found = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '(root)'}: {error.message}"
        for error in found[:_MAX_REPORTED_ERRORS]
    ]


def _total(first: int | None, second: int | None) -> int | None:
    return None if first is None and second is None else (first or 0) + (second or 0)


def _add(first: Usage | None, second: Usage | None) -> Usage | None:
    if first is None:
        return second
    if second is None:
        return first
    return Usage(
        input_tokens=_total(first.input_tokens, second.input_tokens),
        output_tokens=_total(first.output_tokens, second.output_tokens),
    )


async def run_structured(
    chat: ChatCall, request: ChatRequest, *, native: bool, max_repairs: int = 2
) -> ChatResult:
    """Get a validated JSON value for ``request.response_schema`` out of ``chat``.

    With ``native`` the provider is handed the schema; otherwise the schema is put in the prompt.
    In both cases the answer is validated here, and an invalid one is sent back to the model with
    the problems found, up to ``max_repairs`` times.
    """
    schema = request.response_schema
    if schema is None:
        raise InvalidSchemaError("a structured request needs a response schema")
    validator = validator_for(schema)

    messages = tuple(request.messages)
    usage: Usage | None = None
    problems: list[str] = []
    attempts = 1 + max_repairs
    for attempt in range(1, attempts + 1):
        if native:
            sent = request.model_copy(update={"messages": messages})
        else:
            sent = request.model_copy(
                update={
                    "messages": with_json_instructions(messages, schema),
                    "response_schema": None,
                }
            )
        result = await chat(sent)
        usage = _add(usage, result.usage)

        if result.finish_reason is FinishReason.CONTENT_FILTER:
            raise ContentFilteredError("the model declined to answer")
        try:
            value = extract_json(result.text)
        except ValueError:
            problems = ["the answer is not valid JSON"]
        else:
            problems = _problems(validator, value)
            if not problems:
                return ChatResult(
                    text=result.text, parsed=value, finish_reason=result.finish_reason, usage=usage
                )
        if result.finish_reason is FinishReason.LENGTH:
            raise InvalidStructuredOutputError(
                "the answer was cut off by the output limit before the JSON was complete",
                details={"attempts": attempt, "problems": problems},
            )
        messages = repair_messages(request.messages, result.text, problems)

    raise InvalidStructuredOutputError(
        f"the model did not produce a valid answer after {attempts} attempts",
        details={"attempts": attempts, "problems": problems},
    )
