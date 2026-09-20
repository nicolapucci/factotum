"""The worker in this process, where coverage can see it. ``test_knowledge_isolation`` starts it
for real; these call ``main`` with streams instead."""

import dataclasses
import io
import json
import os
import sys
import time
from typing import Any

import pytest

from factotum.knowledge.document import Limits
from factotum.knowledge.loaders import sniff, worker

# Kept before any test replaces it: the limits are for a process about to read a hostile file.
LIMIT_RESOURCES = worker._limit_resources


@pytest.fixture(autouse=True)
def no_resource_limits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Not for the test runner: it would be capped for good."""
    applied: list[tuple[int, int]] = []
    monkeypatch.setattr(
        worker, "_limit_resources", lambda memory, cpu: applied.append((memory, cpu))
    )
    return applied


def request(media_type: str, limits: Limits | None = None, **header: Any) -> bytes:
    return json.dumps(
        {
            "media_type": media_type,
            "limits": dataclasses.asdict(limits or Limits()),
            "memory_bytes": 1 << 30,
            "cpu_seconds": 60,
            **header,
        }
    ).encode()


def run(
    data: bytes, media_type: str = sniff.PLAIN, limits: Limits | None = None, **header: Any
) -> dict[str, Any]:
    output = io.StringIO()
    worker.main(io.BytesIO(request(media_type, limits, **header) + b"\n" + data), output)
    result: dict[str, Any] = json.loads(output.getvalue())
    return result


def test_it_reads_a_document_and_answers_with_it() -> None:
    result = run(b"First.\n\nSecond.")

    assert result["ok"] is True
    assert [block[1] for block in result["blocks"]] == ["First.", "Second."]


def test_the_data_may_contain_anything_including_new_lines_and_nothing_gets_lost() -> None:
    data = b"line one\r\nline two\n\n\xf0\x9f\x98\x80 and bytes: \x00\x01\x02"

    assert run(data)["ok"] is True


def test_what_it_refuses_it_answers_with_the_kind_of_error_and_the_message() -> None:
    result = run(b"a much too long text", limits=Limits(max_chars=5))

    assert result == {
        "ok": False,
        "code": "document_too_large",
        "message": "the text is longer than 5 characters",
    }


def test_a_format_it_does_not_know_is_refused_in_the_same_way() -> None:
    result = run(b"data", "image/png")

    assert result["ok"] is False
    assert result["code"] == "unsupported_document"


def test_the_limits_of_the_process_are_set_before_the_file_is_read(
    no_resource_limits: list[tuple[int, int]],
) -> None:
    run(b"text", memory_bytes=123_456_789, cpu_seconds=42)

    assert no_resource_limits == [(123_456_789, 42)]


class TestTestBehaviours:
    """What a worker does when a test tells it to misbehave: and only then."""

    def test_they_are_off_unless_the_header_says_so(self) -> None:
        result = run(b"x", worker.TEST_UNEXPECTED)

        assert result["ok"] is False
        assert result["code"] == "unsupported_document"

    def test_an_unexpected_failure_is_not_caught(self) -> None:
        with pytest.raises(RuntimeError, match="nobody expected"):
            run(b"x", worker.TEST_UNEXPECTED, testing=True)

    def test_a_crash_is_an_exit_without_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        codes: list[int] = []
        monkeypatch.setattr(os, "_exit", codes.append)

        run(b"x", worker.TEST_CRASH, testing=True)

        assert codes == [139]

    def test_a_hang_is_a_long_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)

        run(b"x", worker.TEST_HANG, testing=True)

        assert slept == [600]

    def test_garbage_is_something_that_is_not_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = request(worker.TEST_GARBAGE, testing=True) + b"\nx"

        with pytest.raises(SystemExit) as stopped:
            worker.main(io.BytesIO(data), io.StringIO())

        assert stopped.value.code == 0
        assert capsys.readouterr().out == "this is not json"


if sys.platform != "win32":  # Windows has no resource limits, and no ``resource`` module
    import resource

    def test_on_a_system_that_has_them_the_limits_are_applied(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[int, tuple[int, int]]] = []
        monkeypatch.setattr(
            resource, "setrlimit", lambda which, limit: calls.append((which, limit))
        )

        LIMIT_RESOURCES(1000, 7)

        assert (resource.RLIMIT_AS, (1000, 1000)) in calls
        assert (resource.RLIMIT_CPU, (7, 7)) in calls
