from collections.abc import Callable

import pytest

from factotum.contracts.errors import DocumentTooLargeError, UnsupportedDocumentError
from factotum.knowledge.document import Limits
from factotum.knowledge.loaders import sniff
from factotum.knowledge.loaders.reader import parse_document

Factory = Callable[..., bytes]


def test_plain_text_is_read_as_plain_text() -> None:
    parsed = parse_document(b"one\n\ntwo", sniff.PLAIN, Limits())

    assert [b.text for b in parsed.blocks] == ["one", "two"]


def test_markdown_is_read_as_markdown() -> None:
    parsed = parse_document(b"# Title\n\nbody", sniff.MARKDOWN, Limits())

    assert [(b.kind, b.text) for b in parsed.blocks] == [("heading", "Title"), ("text", "body")]


def test_html_is_read_as_html() -> None:
    parsed = parse_document(b"<h1>Title</h1><p>body</p>", sniff.HTML, Limits())

    assert [b.kind for b in parsed.blocks] == ["heading", "text"]


def test_a_pdf_is_read_as_a_pdf(pdf: Factory) -> None:
    data = pdf({"Chapter 1": ["Hold the reset button for ten seconds until the light blinks."]})

    assert parse_document(data, sniff.PDF, Limits()).page_count == 1


def test_a_word_document_is_read_as_a_word_document(docx: Factory) -> None:
    assert parse_document(docx(), sniff.DOCX, Limits()).title == "Router guide"


def test_text_in_another_encoding_is_decoded() -> None:
    parsed = parse_document("perché".encode("cp1252"), sniff.PLAIN, Limits())

    assert parsed.text == "perché"


def test_a_format_it_does_not_know_is_refused() -> None:
    with pytest.raises(UnsupportedDocumentError, match="'image/png' is not supported"):
        parse_document(b"data", "image/png", Limits())


def test_a_file_above_the_size_limit_is_refused_before_it_is_opened() -> None:
    with pytest.raises(DocumentTooLargeError, match="larger than 1 MiB"):
        parse_document(b"x" * (1024 * 1024 + 1), sniff.PLAIN, Limits(max_bytes=1024 * 1024))


def test_a_file_at_the_size_limit_is_read() -> None:
    assert parse_document(b"x" * 100, sniff.PLAIN, Limits(max_bytes=100)).text == "x" * 100


def test_text_above_the_length_limit_is_refused() -> None:
    with pytest.raises(DocumentTooLargeError, match="longer than 50 characters"):
        parse_document(b"a very long text " * 10, sniff.PLAIN, Limits(max_chars=50))


def test_a_document_with_no_text_in_it_is_refused() -> None:
    with pytest.raises(UnsupportedDocumentError, match="no text"):
        parse_document(b" \n\n  \n", sniff.PLAIN, Limits())
