"""The reader of documents runs in a process of its own: these tests start real ones."""

import asyncio
import json
import time
from collections.abc import Callable

import pytest

from factotum.contracts.errors import DocumentTooLargeError, UnsupportedDocumentError
from factotum.knowledge.document import Block, Limits, ParsedDocument
from factotum.knowledge.loaders import isolation, sniff, worker
from factotum.knowledge.loaders.isolation import parse_isolated

Factory = Callable[..., bytes]


async def test_a_document_is_read_in_another_process_and_comes_back_whole(docx: Factory) -> None:
    parsed = await parse_isolated(
        docx(table=[["Model", "Button"], ["A1", "left"]]), sniff.DOCX, Limits()
    )

    assert parsed.title == "Router guide"
    heading = next(b for b in parsed.blocks if b.text == "Factory reset")
    assert (heading.kind, heading.level) == ("heading", 2)
    assert heading.headings == ("Resetting", "Factory reset")
    assert any(b.kind == "table" for b in parsed.blocks)


async def test_a_pdf_comes_back_with_its_pages_and_its_outline(pdf: Factory) -> None:
    data = pdf({"Chapter 1": ["Hold the reset button for ten seconds until the light blinks."]})

    parsed = await parse_isolated(data, sniff.PDF, Limits())

    assert parsed.page_count == 1
    assert parsed.blocks[0].page == 1
    assert parsed.blocks[0].headings == ("Chapter 1",)


async def test_text_is_read_the_same_way_as_in_this_process() -> None:
    parsed = await parse_isolated("perché\n\nci vediamo".encode(), sniff.PLAIN, Limits())

    assert [b.text for b in parsed.blocks] == ["perché", "ci vediamo"]


async def test_what_the_reader_refuses_is_refused_with_the_same_error() -> None:
    with pytest.raises(UnsupportedDocumentError, match="no text"):
        await parse_isolated(b"  \n ", sniff.PLAIN, Limits())

    with pytest.raises(DocumentTooLargeError, match="longer than 5 characters"):
        await parse_isolated(b"more than that", sniff.PLAIN, Limits(max_chars=5))


async def test_a_format_that_is_not_supported_is_refused() -> None:
    with pytest.raises(UnsupportedDocumentError, match="not supported"):
        await parse_isolated(b"data", "image/png", Limits())


async def test_a_reader_that_hangs_is_killed_and_the_document_is_refused() -> None:
    started = time.monotonic()

    with pytest.raises(UnsupportedDocumentError, match="took too long"):
        await parse_isolated(b"x", worker.TEST_HANG, Limits(), timeout_s=1.5, testing=True)

    assert time.monotonic() - started < 20


async def test_a_reader_that_crashes_does_not_take_the_service_with_it() -> None:
    with pytest.raises(UnsupportedDocumentError, match="could not be read"):
        await parse_isolated(b"x", worker.TEST_CRASH, Limits(), testing=True)


async def test_a_reader_that_fails_unexpectedly_is_a_document_that_cannot_be_read() -> None:
    with pytest.raises(UnsupportedDocumentError, match="could not be read"):
        await parse_isolated(b"x", worker.TEST_UNEXPECTED, Limits(), testing=True)


async def test_a_reader_that_answers_with_garbage_is_not_believed() -> None:
    with pytest.raises(UnsupportedDocumentError, match="could not be read"):
        await parse_isolated(b"x", worker.TEST_GARBAGE, Limits(), testing=True)


async def test_the_test_behaviours_are_off_unless_they_are_asked_for() -> None:
    with pytest.raises(UnsupportedDocumentError, match="not supported"):
        await parse_isolated(b"x", worker.TEST_CRASH, Limits())


async def test_cancelling_the_read_kills_the_reader() -> None:
    task = asyncio.create_task(
        parse_isolated(b"x", worker.TEST_HANG, Limits(), timeout_s=60, testing=True)
    )
    await asyncio.sleep(1.0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


class TestDecode:
    def encoded(self, **overrides: object) -> bytes:
        parsed = ParsedDocument("Title", (Block("text", "body", 1, ("A",), 0),), 3)
        return json.dumps({**worker.encode(parsed), **overrides}).encode()

    def test_a_document_survives_the_trip(self) -> None:
        parsed = isolation._decode(self.encoded())

        assert parsed == ParsedDocument("Title", (Block("text", "body", 1, ("A",), 0),), 3)

    @pytest.mark.parametrize(
        "output",
        [
            b"",
            b"not json",
            b"[]",
            b"null",
            b'"a string"',
            b'{"ok": true}',
            b'{"ok": true, "title": null, "page_count": null}',
            b'{"ok": true, "title": null, "page_count": null, "blocks": 5}',
            b'{"ok": true, "title": null, "page_count": null, "blocks": [[1]]}',
        ],
    )
    def test_an_answer_that_is_not_a_document_is_refused(self, output: bytes) -> None:
        with pytest.raises(UnsupportedDocumentError, match="could not be read"):
            isolation._decode(output)

    @pytest.mark.parametrize(
        "block",
        [
            ["image", "text", 1, [], 0],
            ["text", 5, 1, [], 0],
            ["text", "body", "1", [], 0],
            ["text", "body", 1, "A", 0],
            ["text", "body", 1, [7], 0],
            ["text", "body", 1, [], "0"],
            ["text", "body", 1, []],
            {"kind": "text"},
        ],
    )
    def test_a_block_with_a_field_of_the_wrong_type_is_refused(self, block: object) -> None:
        with pytest.raises(UnsupportedDocumentError, match="could not be read"):
            isolation._decode(self.encoded(blocks=[block]))

    @pytest.mark.parametrize("field", [{"title": 5}, {"page_count": "3"}])
    def test_a_title_or_a_page_count_of_the_wrong_type_is_refused(
        self, field: dict[str, object]
    ) -> None:
        with pytest.raises(UnsupportedDocumentError, match="could not be read"):
            isolation._decode(self.encoded(**field))

    def test_an_error_keeps_its_kind_and_its_message(self) -> None:
        output = json.dumps({"ok": False, "code": "document_too_large", "message": "too big"})

        with pytest.raises(DocumentTooLargeError, match="too big"):
            isolation._decode(output.encode())

    def test_an_error_of_a_kind_it_does_not_know_is_an_unsupported_document(self) -> None:
        output = json.dumps({"ok": False, "code": "something_else", "message": "odd"})

        with pytest.raises(UnsupportedDocumentError, match="odd"):
            isolation._decode(output.encode())


class TestFailure:
    def test_only_the_exception_that_ended_the_reader_is_kept(self) -> None:
        stderr = b"Traceback (most recent call last):\n  File x\nValueError: the secret is 42\n"

        assert isolation._failure(stderr) == "ValueError"

    def test_nothing_printed_is_nothing_kept(self) -> None:
        assert isolation._failure(b"") == ""
        assert isolation._failure(b" \n \n") == ""

    def test_it_is_short_and_survives_bytes_that_are_not_text(self) -> None:
        assert len(isolation._failure(b"x" * 500)) == 100
        assert isolation._failure(b"\xff\xfe boom") != ""


class TestEnvironment:
    def test_the_reader_gets_no_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FACTOTUM_POSTGRES_PASSWORD", "hunter2")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("PATH", "/bin")

        environment = isolation._environment()

        assert environment["PATH"] == "/bin"
        assert "FACTOTUM_POSTGRES_PASSWORD" not in environment
        assert "OPENAI_API_KEY" not in environment
        assert set(environment) <= set(isolation._ENVIRONMENT_ALLOWED)

    def test_a_variable_that_is_not_set_is_not_invented(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LC_ALL", raising=False)

        assert "LC_ALL" not in isolation._environment()


class TestWorkerEncoding:
    def test_a_document_is_encoded_with_everything_the_service_needs(self) -> None:
        parsed = ParsedDocument("T", (Block("heading", "H", 2, (), 1),), None)

        assert worker.encode(parsed) == {
            "ok": True,
            "title": "T",
            "page_count": None,
            "blocks": [["heading", "H", 2, [], 1]],
        }
