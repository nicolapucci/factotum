"""The program that runs a plan: what the platform does in the sandbox in place of a script.

A skill in instructions mode has no script. A model wrote a plan, the platform checked it
(``factotum.actions.instructions.checks``), and this is what runs it: in the container of the run,
with the network the skill was granted and the secrets it was granted, exactly as a script would.
It does what the plan says, step by step, and nothing else. It has no way to run what a plan holds,
because a plan holds no code: it is data, and this reads it.

It is a single file of the standard library, because it is put into the container as it is, and
because the checks and the runner must agree on what a plan means: the same functions that read a
placeholder here are used by the checks before anything is sent.

Input, as JSON on standard input::

    {"plan": {...}, "params": {...}, "device_params": {...}, "secrets": ["NAME", ...],
     "budget_s": 20}

Output, as one JSON document on standard output (what every script answers)::

    {"message": "...", "data": {"steps": {"id": {"status": 200}}}, "commands": [...]}

A request that does not give what it should (a status of 300 or more, a connection that failed, a
value that is not there) ends the run with exit code 1 and one line on standard error that says
which step and why. What a plan says to say is not worth failing for, once the requests were made: a
sentence that cannot be worked out is left unsaid.
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

MAX_RESPONSE_BYTES = 1_000_000
"""The most of an answer that is read: more is an error, not a reason to fill the memory."""

MAX_TEXT_CHARS = 65_536
"""How much of the text of an answer is kept for a plan to refer to."""

MAX_MESSAGE_CHARS = 2000
REQUEST_TIMEOUT_S = 10.0
USER_AGENT = "Factotum/1 (instructions)"

KINDS = ("secret", "device", "value", "step")
FILTERS = ("round",)
STEP_FIELDS = ("status", "text", "json")

_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_VALUE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class LanguageError(ValueError):
    """Something in a plan is not what the language of plans says. The message says what is wrong
    and never what a secret is."""


class StepError(Exception):
    """A step could not be done."""


@dataclass(frozen=True, slots=True)
class Ref:
    """A placeholder, read: what it refers to."""

    kind: str
    """``secret``, ``device``, ``value`` or ``step``."""

    name: str
    """The secret, the value of the device or the value that was asked for, or the step."""

    path: tuple[str, ...] = ()
    """For a step: ``("status",)``, ``("text",)`` or ``("json", key, key, ...)``."""

    filter: str | None = None
    raw: str = ""


def _clipped(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_ref(inner: str) -> Ref:
    """The placeholder that ``${inner}`` is. Raises ``LanguageError`` when it is not one."""
    raw = "${" + inner + "}"
    body, bar, filter_name = inner.partition("|")
    if bar and filter_name not in FILTERS:
        raise LanguageError(f"{_clipped(raw)} has a filter that is not one of {', '.join(FILTERS)}")
    parts = body.split(".")
    kind = parts[0]
    if kind not in KINDS:
        raise LanguageError(
            f"{_clipped(raw)} is not a placeholder: it starts with {', '.join(KINDS)}"
        )
    if kind in {"secret", "device", "value"}:
        pattern = _SECRET_NAME if kind == "secret" else _VALUE_NAME
        if len(parts) != 2 or not pattern.fullmatch(parts[1]):
            raise LanguageError(f"{_clipped(raw)} does not name a {kind}")
        return Ref(kind, parts[1], (), filter_name or None, raw)
    if len(parts) < 3 or not _STEP_ID.fullmatch(parts[1]) or parts[2] not in STEP_FIELDS:
        raise LanguageError(f"{_clipped(raw)} does not say a step and status, text or json")
    if parts[2] != "json" and len(parts) != 3:
        raise LanguageError(f"{_clipped(raw)} has a path, and only json has one")
    if not all(_SEGMENT.fullmatch(segment) for segment in parts[3:]):
        raise LanguageError(f"{_clipped(raw)} has a path that is not keys and positions")
    return Ref("step", parts[1], tuple(parts[2:]), filter_name or None, raw)


def find_refs(text: str) -> list[Ref]:
    """Every placeholder in a text, read. Raises ``LanguageError`` for one that is not a
    placeholder, and for a ``${`` that does not close."""
    found = [parse_ref(match.group(1)) for match in _PLACEHOLDER.finditer(text)]
    if "${" in _PLACEHOLDER.sub("", text):
        raise LanguageError("a placeholder is not closed")
    return found


def strings_of(value: Any) -> Iterator[str]:
    """Every text a JSON value holds as a value (not as the name of a field)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings_of(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_of(item)


def names_of(value: Any) -> Iterator[str]:
    """Every name of a field that a JSON value holds, at any depth."""
    if isinstance(value, list):
        for item in value:
            yield from names_of(item)
    elif isinstance(value, dict):
        for name, item in value.items():
            yield str(name)
            yield from names_of(item)


@dataclass(frozen=True, slots=True)
class StepResult:
    status: int
    text: str
    document: Any = None
    has_document: bool = False


class Context:
    """What a placeholder can be worth: the secrets it may be given, what the device is, what the
    person asked for and what the earlier steps got."""

    def __init__(
        self,
        *,
        secrets: Mapping[str, str],
        device: Mapping[str, Any],
        values: Mapping[str, Any],
        secrets_allowed: bool = True,
    ) -> None:
        self._secrets = secrets
        self._device = device
        self._values = values
        self.steps: dict[str, StepResult] = {}
        self.secrets_allowed = secrets_allowed

    def without_secrets(self) -> "Context":
        """The same, for what is said or asked of a device: a secret is never one of those."""
        other = Context(
            secrets=self._secrets, device=self._device, values=self._values, secrets_allowed=False
        )
        other.steps = self.steps
        return other

    def lookup(self, ref: Ref) -> Any:
        value = self._lookup(ref)
        if ref.filter == "round":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise LanguageError(f"{_clipped(ref.raw)} is not a number to round")
            return math.floor(value + 0.5)  # half goes up, as a person rounds
        return value

    def _lookup(self, ref: Ref) -> Any:
        if ref.kind == "secret":
            if not self.secrets_allowed:
                raise LanguageError("a secret is not for saying")
            if ref.name not in self._secrets:
                raise LanguageError(f"the secret {ref.name} is not there")
            return self._secrets[ref.name]
        if ref.kind in {"device", "value"}:
            source = self._device if ref.kind == "device" else self._values
            if ref.name not in source:
                raise LanguageError(f"there is no {ref.kind} called {ref.name}")
            return source[ref.name]
        result = self.steps.get(ref.name)
        if result is None:
            raise LanguageError(f"the step {ref.name} has not been done")
        field = ref.path[0]
        if field == "status":
            return result.status
        if field == "text":
            return result.text
        if not result.has_document:
            raise LanguageError(f"the answer of {ref.name} is not JSON")
        found: Any = result.document
        for segment in ref.path[1:]:
            if isinstance(found, dict) and segment in found:
                found = found[segment]
            elif isinstance(found, list) and segment.isdecimal() and int(segment) < len(found):
                found = found[int(segment)]
            else:
                raise LanguageError(f"the answer of {ref.name} has nothing at {segment}")
        return found


def as_text(value: Any) -> str:
    """A value as it is written into a text."""
    if value is None:
        raise LanguageError("a value that is nothing cannot be written")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def substitute_text(text: str, context: Context, *, quote: bool = False) -> str:
    """A text with what its placeholders are worth put in. With ``quote``, for a place in an
    address, what is put in is written so that it cannot end the part it is in."""

    def worth(match: re.Match[str]) -> str:
        written = as_text(context.lookup(parse_ref(match.group(1))))
        return urllib.parse.quote(written, safe="") if quote else written

    return _PLACEHOLDER.sub(worth, text)


def substitute_json(value: Any, context: Context) -> Any:
    """A JSON value with what its placeholders are worth put in. A text that is one placeholder
    and nothing else becomes the value itself, with its type: a number stays a number."""
    if isinstance(value, str):
        alone = _PLACEHOLDER.fullmatch(value)
        if alone is not None:
            return context.lookup(parse_ref(alone.group(1)))
        return substitute_text(value, context)
    if isinstance(value, list):
        return [substitute_json(item, context) for item in value]
    if isinstance(value, dict):
        return {name: substitute_json(item, context) for name, item in value.items()}
    return value


def split_url(url: str) -> tuple[str, str, str] | None:
    """The scheme, the authority (whatever is between ``//`` and the path) and the rest of an
    address, or nothing when it does not look like one."""
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)(.*)", url, re.DOTALL)
    return None if match is None else (match.group(1), match.group(2), match.group(3))


# ---- doing it ------------------------------------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """An answer that says to go somewhere else is an answer, and the plan did not say to go."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _has(headers: Mapping[str, str], name: str) -> bool:
    return any(key.lower() == name for key in headers)


def request(step: Mapping[str, Any], context: Context, remaining_s: float) -> StepResult:
    """One request of a plan, made through the proxy that the environment says to use."""
    method = str(step.get("method"))
    raw_url = step.get("url")
    if not isinstance(raw_url, str) or method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise StepError("it is not a request")
    url = urllib.parse.quote(
        substitute_text(raw_url, context, quote=True), safe=":/?#[]@!$&'()*+,;=%"
    )
    headers = {
        str(k): substitute_text(str(v), context) for k, v in dict(step.get("headers") or {}).items()
    }
    body = step.get("body")
    data: bytes | None = None
    if body is not None:
        if method == "GET":
            raise StepError("a GET has no body")
        if isinstance(body, str):
            data = substitute_text(body, context).encode("utf-8")
            kind = "text/plain; charset=utf-8"
        else:
            data = json.dumps(substitute_json(body, context), ensure_ascii=False).encode("utf-8")
            kind = "application/json"
        if not _has(headers, "content-type"):
            headers["Content-Type"] = kind
    if not _has(headers, "user-agent"):
        headers["User-Agent"] = USER_AGENT
    opener = urllib.request.build_opener(_NoRedirects)
    prepared = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    timeout = min(REQUEST_TIMEOUT_S, remaining_s)
    try:
        with opener.open(prepared, timeout=timeout) as response:
            status, raw = int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as error:
        status, raw = int(error.code), error.read(MAX_RESPONSE_BYTES + 1)
        charset = error.headers.get_content_charset() or "utf-8"
    except (urllib.error.URLError, OSError, ValueError) as error:
        # A URLError says why in its own words (a refusal of the proxy, a name that is not found).
        reason = getattr(error, "reason", None) or error
        raise StepError(f"the request did not get through ({type(reason).__name__})") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise StepError("the answer is too big")
    if status >= 300:
        raise StepError(f"the answer is {status}")
    text = raw.decode(charset if _is_a_charset(charset) else "utf-8", errors="replace")
    try:
        return StepResult(status, text[:MAX_TEXT_CHARS], json.loads(text), True)
    except ValueError:
        return StepResult(status, text[:MAX_TEXT_CHARS])


def _is_a_charset(name: str) -> bool:
    try:
        "".encode(name)
    except LookupError:
        return False
    return True


def command(step: Mapping[str, Any], context: Context) -> dict[str, Any]:
    """A command for a device, with what its placeholders are worth put in."""
    name = step.get("name")
    args = substitute_json(dict(step.get("args") or {}), context.without_secrets())
    if not isinstance(name, str) or not isinstance(args, dict):
        raise StepError("it is not a command")
    return {"name": name, **args}


def run(
    plan: Mapping[str, Any],
    *,
    context: Context,
    budget_s: float,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """What a plan comes to: the message, what the requests said, and the commands."""
    deadline = now() + budget_s
    commands: list[dict[str, Any]] = []
    steps = plan.get("steps") or []
    if not isinstance(steps, list):
        raise StepError("the plan: it has no steps")
    for step in steps:
        identifier = str(step.get("id")) if isinstance(step, dict) else "?"
        try:
            if not isinstance(step, dict):
                raise StepError("it is not a step")
            if step.get("kind") == "http":
                remaining = deadline - now()
                if remaining <= 0:
                    raise StepError("there is no time left")
                context.steps[identifier] = request(step, context, remaining)
            elif step.get("kind") == "command":
                commands.append(command(step, context))
            else:
                raise StepError("it is neither a request nor a command")
        except (LanguageError, StepError) as error:
            raise StepError(f"{identifier}: {error}") from None
    return {
        "message": _said(plan.get("say"), context),
        "data": {"steps": {name: {"status": got.status} for name, got in context.steps.items()}},
        "commands": commands,
    }


def _said(template: Any, context: Context) -> str | None:
    """The sentence, or nothing when it cannot be worked out: it is not worth failing for once the
    requests were made."""
    if not isinstance(template, str) or not template.strip():
        return None
    try:
        sentence = substitute_text(template, context.without_secrets()).strip()
    except LanguageError as error:
        sys.stderr.write(f"what to say could not be worked out: {error}\n")
        return None
    return sentence[:MAX_MESSAGE_CHARS] or None


def main() -> int:
    try:
        given = json.load(sys.stdin)
        plan = given["plan"]
        names = [str(name) for name in given.get("secrets") or []]
        context = Context(
            secrets={name: os.environ[name] for name in names if name in os.environ},
            device=dict(given.get("device_params") or {}),
            values=dict(given.get("params") or {}),
        )
        budget = float(given.get("budget_s") or 20)
    except (ValueError, KeyError, TypeError, AttributeError):
        sys.stderr.write("what was given is not a plan to run\n")
        return 2
    try:
        answer = run(plan, context=context, budget_s=budget)
    except StepError as error:
        sys.stderr.write(f"step {error}\n")
        return 1
    sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
