import io
import json
import logging
from collections.abc import Iterator

import pytest

from factotum.common.logs import configure_logging
from factotum.common.redaction import REDACTED


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def lines(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line]


def one_record(stream: io.StringIO) -> dict[str, object]:
    (line,) = lines(stream)
    parsed: dict[str, object] = json.loads(line)
    return parsed


def test_a_record_is_one_json_object_with_the_basics() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)

    logging.getLogger("factotum.test").info("hello")

    record = one_record(stream)
    assert record["message"] == "hello"
    assert record["level"] == "INFO"
    assert record["logger"] == "factotum.test"
    assert "timestamp" in record


def test_secrets_in_the_message_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    logging.getLogger("t").warning("calling https://api.example.org/v1?key=TOPSECRET123")

    assert "TOPSECRET123" not in stream.getvalue()
    assert f"key={REDACTED}" in str(one_record(stream)["message"])


def test_secrets_passed_as_format_arguments_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    logging.getLogger("t").info("using %s for %s", "Bearer abcdef1234567890", "tenant-1")

    assert "abcdef1234567890" not in stream.getvalue()
    assert "tenant-1" in stream.getvalue()


def test_extra_fields_are_redacted_by_key_and_by_shape() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    logging.getLogger("t").info(
        "request",
        extra={
            "api_key": "sk-abcdefghijklmnop1234",
            "headers": {"Authorization": "Bearer abcdef1234567890"},
            "model": "llama",
        },
    )

    record = one_record(stream)
    assert record["api_key"] == REDACTED
    assert record["headers"] == {"Authorization": REDACTED}
    assert record["model"] == "llama"


def test_tracebacks_are_redacted_too() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    try:
        raise RuntimeError("upstream said no for https://api.example.org/?key=TOPSECRET123")
    except RuntimeError:
        logging.getLogger("t").exception("failed")

    assert "TOPSECRET123" not in stream.getvalue()
    assert "RuntimeError" in str(one_record(stream)["exception"])


def test_a_bad_format_string_does_not_break_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest's own capture handler re-raises formatting errors on purpose; in production the
    # standard handlers report them on stderr and go on, which is what is simulated here.
    monkeypatch.setattr(logging, "raiseExceptions", False)
    stream = io.StringIO()
    configure_logging(stream=stream)

    logging.getLogger("t").info("needs two: %s %s", "only-one")

    assert lines(stream)


def test_the_level_filters_records() -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)

    logging.getLogger("t").info("quiet")
    logging.getLogger("t").error("loud")

    assert [json.loads(line)["message"] for line in lines(stream)] == ["loud"]


def test_the_text_format_is_redacted_as_well() -> None:
    stream = io.StringIO()
    configure_logging(json_format=False, stream=stream)

    logging.getLogger("t").info("token is Bearer abcdef1234567890")

    assert "abcdef1234567890" not in stream.getvalue()
    assert "INFO" in stream.getvalue()


def test_configuring_twice_does_not_duplicate_output() -> None:
    first, second = io.StringIO(), io.StringIO()

    configure_logging(stream=first)
    configure_logging(stream=second)
    logging.getLogger("t").info("once")

    assert first.getvalue() == ""
    assert len(lines(second)) == 1
