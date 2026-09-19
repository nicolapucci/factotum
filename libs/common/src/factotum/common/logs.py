"""Structured logging to stdout, with secrets redacted before anything is written.

Redaction happens in a filter attached to the handler, so it covers every logger in the process,
libraries included, and both the message and the extra fields. Exception tracebacks are redacted
too, because errors raised by HTTP clients like to quote the URL and the headers.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from factotum.common.redaction import redact, redact_text

_STANDARD_ATTRIBUTES = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class RedactingFilter(logging.Filter):
    """Masks secrets in the message and in every extra field of a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break logging
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = None
        for name in [name for name in record.__dict__ if name not in _STANDARD_ATTRIBUTES]:
            record.__dict__[name] = redact(record.__dict__[name], key=name)
        return True


class _RedactingTracebackMixin(logging.Formatter):
    def formatException(self, ei: Any) -> str:  # noqa: N802 - stdlib name
        return redact_text(super().formatException(ei))


class JsonFormatter(_RedactingTracebackMixin):
    """One JSON object per line: timestamp, level, logger, message and any extra field."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name, value in record.__dict__.items():
            if name not in _STANDARD_ATTRIBUTES:
                payload.setdefault(name, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _render(value: Any) -> str:
    """A value on one line: bare when it is a plain word or number, quoted when it is not, so that
    a value with a line break in it cannot pass for a second record."""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    if text and text.isprintable() and " " not in text:
        return text
    return json.dumps(text, ensure_ascii=False)


class TextFormatter(_RedactingTracebackMixin):
    """A human-friendly format for development: the message, then the extra fields as key=value."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 - stdlib name
        line = super().formatMessage(record)
        fields = " ".join(
            f"{name}={_render(value)}"
            for name, value in record.__dict__.items()
            if name not in _STANDARD_ATTRIBUTES
        )
        return f"{line} {fields}" if fields else line


class _FactotumHandler(logging.StreamHandler[TextIO]):
    """Marks the handler installed by ``configure_logging`` so that it can be replaced."""


def configure_logging(
    *, level: str = "INFO", json_format: bool = True, stream: TextIO | None = None
) -> logging.Handler:
    """Send the root logger to ``stream`` (stdout by default), redacted.

    Calling it again replaces the previous configuration instead of duplicating output.
    """
    handler = _FactotumHandler(stream if stream is not None else sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_format else TextFormatter())

    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _FactotumHandler)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    return handler
