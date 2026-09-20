import io
import zipfile

import pytest

from factotum.contracts.errors import UnsupportedDocumentError
from factotum.knowledge.loaders import sniff


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class TestDetectMediaType:
    def test_a_pdf_is_recognized_by_its_header_whatever_it_is_called(self) -> None:
        assert sniff.detect_media_type(b"%PDF-1.7\n...", filename="notes.txt") == sniff.PDF

    def test_a_pdf_may_have_junk_before_its_header(self) -> None:
        assert sniff.detect_media_type(b"\r\n  junk \n%PDF-1.4\n") == sniff.PDF

    def test_a_word_document_is_a_zip_with_a_document_part(self) -> None:
        archive = zipped({"word/document.xml": b"<w/>", "[Content_Types].xml": b"<t/>"})

        assert sniff.detect_media_type(archive, filename="x.zip") == sniff.DOCX

    def test_any_other_archive_is_refused(self) -> None:
        with pytest.raises(UnsupportedDocumentError, match="archives are not supported"):
            sniff.detect_media_type(zipped({"readme.txt": b"hello"}))

    def test_a_damaged_archive_is_refused(self) -> None:
        with pytest.raises(UnsupportedDocumentError, match="damaged archive"):
            sniff.detect_media_type(b"PK\x03\x04 this is not a zip")

    @pytest.mark.parametrize(
        "html",
        [
            b"<!DOCTYPE html><html><body>hi</body></html>",
            b"  \n<html lang='en'><head></head></html>",
            b"<HTML><BODY>shouting</BODY></HTML>",
            b"\xef\xbb\xbf<!doctype html><p>with a BOM</p>",
            b"<head><title>x</title></head><body>y</body>",
        ],
    )
    def test_a_page_is_recognized_by_how_it_starts(self, html: bytes) -> None:
        assert sniff.detect_media_type(html) == sniff.HTML

    def test_text_that_only_mentions_html_is_not_html(self) -> None:
        text = b"To start a page, write <html> and then <body>."

        assert sniff.detect_media_type(text) == sniff.PLAIN

    def test_a_fragment_is_html_when_it_is_called_so(self) -> None:
        assert sniff.detect_media_type(b"<p>a fragment</p>", filename="page.HTM") == sniff.HTML

    def test_a_name_that_says_html_is_not_enough_without_markup(self) -> None:
        assert sniff.detect_media_type(b"just words", filename="page.html") == sniff.PLAIN

    @pytest.mark.parametrize("filename", ["notes.md", "README.MARKDOWN"])
    def test_markdown_is_told_by_the_name(self, filename: str) -> None:
        assert sniff.detect_media_type(b"# Title\n", filename=filename) == sniff.MARKDOWN

    def test_markdown_is_told_by_the_declared_type(self) -> None:
        assert sniff.detect_media_type(b"# T", declared="text/markdown") == sniff.MARKDOWN

    def test_anything_else_that_is_text_is_plain_text(self) -> None:
        assert sniff.detect_media_type(b"just some words", filename="a.txt") == sniff.PLAIN

    def test_the_declared_type_cannot_turn_a_binary_into_text(self) -> None:
        with pytest.raises(UnsupportedDocumentError, match="not supported"):
            sniff.detect_media_type(b"\x89PNG\r\n\x1a\n\x00\x00\x00", declared="text/plain")

    def test_the_declared_type_cannot_turn_text_into_a_pdf(self) -> None:
        assert sniff.detect_media_type(b"not a pdf", declared="application/pdf") == sniff.PLAIN

    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(UnsupportedDocumentError, match="empty"):
            sniff.detect_media_type(b"")

    def test_utf16_text_is_text_and_not_binary(self) -> None:
        assert sniff.detect_media_type("hello".encode("utf-16")) == sniff.PLAIN

    def test_every_format_it_can_return_is_a_supported_one(self) -> None:
        assert {sniff.PDF, sniff.DOCX, sniff.HTML, sniff.MARKDOWN, sniff.PLAIN} == sniff.SUPPORTED


class TestDecodeText:
    def test_utf8(self) -> None:
        assert sniff.decode_text("perché".encode()) == "perché"

    def test_utf8_with_a_bom(self) -> None:
        assert sniff.decode_text(b"\xef\xbb\xbfhello") == "hello"

    def test_utf16_with_a_bom(self) -> None:
        assert sniff.decode_text("città".encode("utf-16")) == "città"

    def test_windows_1252_when_it_is_not_utf8(self) -> None:
        assert sniff.decode_text("perché".encode("cp1252")) == "perché"

    def test_it_never_raises_on_bytes_that_are_nothing(self) -> None:
        assert isinstance(sniff.decode_text(b"\xff\xfe\x00\x81\x8d"), str)
