"""Keeping secrets out of logs, error messages and audit records.

A value is redacted by its *key* (anything stored under ``password``, ``authorization``,
``x-api-key``...) and by its *shape* (a bearer token, an API key with a well-known prefix,
credentials embedded in a URL, a ``key=`` query parameter). Redaction is idempotent and never
raises: it is used on the error paths, where a second failure would hide the first.
"""

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

_MAX_DEPTH = 12

# Compared against the key lowercased and stripped of everything that is not a letter or a digit,
# so "X-Api-Key", "x_api_key" and "xApiKey" are the same. Plural forms such as "max_tokens" or
# "secrets" (names, not values) deliberately do not match.
_SENSITIVE_KEY_SUFFIXES = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "privatekey",
    "accesskey",
    "secretkey",
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # "Authorization: Bearer abc..." and "Basic abc..."
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # ?key=..., &api_key=..., &token=...
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|key|token|access_token|auth|password|secret|sig|signature)=)"
            r"[^&\s#]+"
        ),
        r"\1" + REDACTED,
    ),
    # https://user:password@host
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+(@)"), r"\1" + REDACTED + r"\2"),
    # Keys with a well-known shape.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),  # OpenAI, Anthropic
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), REDACTED),  # Google
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), REDACTED),  # GitHub
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),  # Slack
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),  # JWT
)


def is_sensitive_key(key: str) -> bool:
    """Whether a value stored under ``key`` must never be shown."""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def redact_text(text: str) -> str:
    """Mask the secrets that can be recognized by their shape inside free text."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """A copy of ``value`` with secrets masked; the input is never modified.

    Works on strings, mappings, lists, tuples and sets. ``key`` is the name the value is stored
    under, when known.
    """
    if key is not None and is_sensitive_key(key):
        return None if value is None else REDACTED
    if _depth >= _MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        return {
            str(name): redact(item, key=str(name), _depth=_depth + 1)
            for name, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        items = [redact(item, _depth=_depth + 1) for item in value]
        return items if isinstance(value, list) else type(value)(items)
    return value
