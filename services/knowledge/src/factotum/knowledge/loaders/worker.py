"""The process that reads a document: ``python -m factotum.knowledge.loaders.worker``.

It gets the document on its standard input, after one line of JSON that says the format and the
limits, and answers with one JSON document on its standard output. It exists so that a file that
hangs the parser, exhausts memory or crashes it takes down this process and not the service; the
service kills it when it takes too long (``isolation``).
"""

import json
import os
import sys
import time
from typing import Any, BinaryIO, TextIO

from factotum.contracts.errors import FactotumError
from factotum.knowledge.document import Limits, ParsedDocument
from factotum.knowledge.loaders.reader import parse_document

TEST_HANG = "application/x-factotum-test-hang"
TEST_CRASH = "application/x-factotum-test-crash"
TEST_GARBAGE = "application/x-factotum-test-garbage"
TEST_UNEXPECTED = "application/x-factotum-test-unexpected"


def encode(parsed: ParsedDocument) -> dict[str, Any]:
    return {
        "ok": True,
        "title": parsed.title,
        "page_count": parsed.page_count,
        "blocks": [
            [block.kind, block.text, block.page, list(block.headings), block.level]
            for block in parsed.blocks
        ],
    }


def _limit_resources(max_memory_bytes: int, cpu_seconds: int) -> None:
    """Where the operating system allows it, cap what a hostile file can take."""
    if sys.platform == "win32":  # pragma: no cover - the platform has no rlimits
        return
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))


def _test_behaviour(media_type: str) -> None:
    """What the tests ask of a worker that is told to misbehave. Off unless the header says so."""
    match media_type:
        case "application/x-factotum-test-hang":
            time.sleep(600)
        case "application/x-factotum-test-crash":
            os._exit(139)
        case "application/x-factotum-test-garbage":
            sys.stdout.write("this is not json")
            sys.stdout.flush()
            raise SystemExit(0)
        case "application/x-factotum-test-unexpected":
            raise RuntimeError("something nobody expected")


def main(stdin: BinaryIO, stdout: TextIO) -> None:
    header = json.loads(stdin.readline())
    data = stdin.read()
    limits = Limits(**header["limits"])
    media_type: str = header["media_type"]

    _limit_resources(header["memory_bytes"], header["cpu_seconds"])
    if header.get("testing"):
        _test_behaviour(media_type)

    try:
        result: dict[str, Any] = encode(parse_document(data, media_type, limits))
    except FactotumError as error:
        result = {"ok": False, "code": error.code, "message": error.message}
    stdout.write(json.dumps(result))


if __name__ == "__main__":
    main(sys.stdin.buffer, sys.stdout)
