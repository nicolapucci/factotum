"""Reading a document in a process that can be killed.

The parsers are the riskiest code of the service: they read files nobody vetted, in formats with a
history of decompression bombs and infinite loops. Each document is read by a fresh Python process
with a time limit, a memory limit where the system allows one, and none of the service's secrets in
its environment. If it hangs, it is killed; if it crashes, the service does not notice.
"""

import asyncio
import dataclasses
import json
import logging
import os
import sys

from factotum.contracts.errors import (
    DocumentTooLargeError,
    FactotumError,
    UnsupportedDocumentError,
)
from factotum.knowledge.document import Block, Limits, ParsedDocument

logger = logging.getLogger("factotum.knowledge")

DEFAULT_TIMEOUT_S = 300.0
DEFAULT_MEMORY_BYTES = 3 * 1024 * 1024 * 1024

# The worker needs its interpreter and its libraries and nothing else: no keys, no passwords.
_ENVIRONMENT_ALLOWED = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)

_KINDS = {"heading", "text", "code", "table"}

_ERRORS: dict[str, type[FactotumError]] = {
    UnsupportedDocumentError.code: UnsupportedDocumentError,
    DocumentTooLargeError.code: DocumentTooLargeError,
}


def _environment() -> dict[str, str]:
    return {name: os.environ[name] for name in _ENVIRONMENT_ALLOWED if name in os.environ}


def _block(raw: object) -> Block:
    if not isinstance(raw, list) or len(raw) != 5:
        raise ValueError("a block has five fields")
    kind, text, page, headings, level = raw
    if (
        kind not in _KINDS
        or not isinstance(text, str)
        or not (page is None or isinstance(page, int))
        or not (isinstance(headings, list) and all(isinstance(item, str) for item in headings))
        or not isinstance(level, int)
    ):
        raise ValueError("a block has a field of the wrong type")
    return Block(kind, text, page, tuple(headings), level)


def _decode(output: bytes) -> ParsedDocument:
    """What the worker wrote, as a document. The worker read a hostile file, so its answer is
    checked like any other input."""
    try:
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError("the answer is not an object")
        if not result.get("ok"):
            error = _ERRORS.get(str(result.get("code")), UnsupportedDocumentError)
            raise error(str(result.get("message") or "the file could not be read"))
        title, page_count = result["title"], result["page_count"]
        if not (title is None or isinstance(title, str)) or not (
            page_count is None or isinstance(page_count, int)
        ):
            raise ValueError("the title or the page count has the wrong type")
        return ParsedDocument(title, tuple(_block(raw) for raw in result["blocks"]), page_count)
    except FactotumError:
        raise
    except (ValueError, KeyError, TypeError):
        logger.warning("the document reader answered with something that is not a document")
        raise UnsupportedDocumentError("the file could not be read") from None


def _failure(stderr: bytes) -> str:
    """Which exception ended the worker, and nothing else it printed: a traceback may quote the
    document."""
    lines = [line for line in stderr.decode(errors="replace").splitlines() if line.strip()]
    return lines[-1].split(":")[0][:100] if lines else ""


async def parse_isolated(
    data: bytes,
    media_type: str,
    limits: Limits,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    testing: bool = False,
) -> ParsedDocument:
    """Read ``data`` in a separate process. Raises ``UnsupportedDocumentError`` or
    ``DocumentTooLargeError`` for a document that cannot or may not be read, and never anything
    else because of the document."""
    header = {
        "media_type": media_type,
        "limits": dataclasses.asdict(limits),
        "memory_bytes": memory_bytes,
        "cpu_seconds": int(timeout_s) + 30,
        "testing": testing,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "factotum.knowledge.loaders.worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_environment(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(header).encode() + b"\n" + data), timeout_s
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise UnsupportedDocumentError("the file took too long to read") from None
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0:
        logger.warning(
            "the document reader failed",
            extra={"exit_code": process.returncode, "failure": _failure(stderr)},
        )
        raise UnsupportedDocumentError("the file could not be read")
    return _decode(stdout)
