"""Which format a file is, from what is in it and not from what it is called."""

import codecs
import io
import zipfile

from factotum.contracts.errors import UnsupportedDocumentError

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HTML = "text/html"
MARKDOWN = "text/markdown"
PLAIN = "text/plain"

SUPPORTED = frozenset({PDF, DOCX, HTML, MARKDOWN, PLAIN})

_BOMS = (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE)
_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body")


def decode_text(data: bytes) -> str:
    """Text from bytes: UTF-8 (with or without a BOM), UTF-16 with a BOM, else Windows-1252."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head and not head.startswith(_BOMS)


def detect_media_type(data: bytes, filename: str | None = None, declared: str | None = None) -> str:
    """The format of a file. Raises ``UnsupportedDocumentError`` for one that cannot be read.

    What the uploader said (the name, the declared type) only decides between formats that are
    otherwise the same thing: Markdown or plain text.
    """
    if not data:
        raise UnsupportedDocumentError("the file is empty")
    if b"%PDF-" in data[:1024]:
        return PDF
    if data.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            raise UnsupportedDocumentError("the file is a damaged archive") from None
        if "word/document.xml" in names:
            return DOCX
        raise UnsupportedDocumentError("archives are not supported, only Word documents (.docx)")
    if _is_binary(data[:8192]):
        raise UnsupportedDocumentError("this file format is not supported")

    head = data[:2048].lstrip(codecs.BOM_UTF8).lstrip().lower()
    if head.startswith(_HTML_MARKERS):
        return HTML
    name = (filename or "").lower()
    if name.endswith((".md", ".markdown")) or (declared or "").lower() == MARKDOWN:
        return MARKDOWN
    if name.endswith((".html", ".htm")) and b"<" in head:
        return HTML
    return PLAIN
