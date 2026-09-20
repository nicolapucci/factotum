"""Word documents (.docx)."""

import io
import re
import zipfile

from docx import Document as WordDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from factotum.contracts.errors import DocumentTooLargeError, UnsupportedDocumentError
from factotum.knowledge.document import Block, Limits, ParsedDocument, apply_headings

_HEADING = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)
_MAX_RATIO = 200
_MAX_ENTRIES = 20_000


def _check_archive(data: bytes, limits: Limits) -> None:
    """A .docx is a zip archive: one that expands to gigabytes is an attack, not a document."""
    try:
        entries = zipfile.ZipFile(io.BytesIO(data)).infolist()
    except zipfile.BadZipFile:
        raise UnsupportedDocumentError("the file is a damaged Word document") from None
    if len(entries) > _MAX_ENTRIES:
        raise DocumentTooLargeError("the Word document holds too many parts")
    if sum(entry.file_size for entry in entries) > limits.max_zip_bytes:
        raise DocumentTooLargeError("the Word document expands to too much")
    if any(entry.file_size > _MAX_RATIO * max(entry.compress_size, 1) + 1024 for entry in entries):
        raise DocumentTooLargeError("the Word document is compressed suspiciously well")


def _heading_level(style: str) -> int:
    if style.lower() == "title":
        return 1
    matched = _HEADING.match(style)
    return int(matched.group(1)) if matched else 0


def parse_docx(data: bytes, limits: Limits) -> ParsedDocument:
    _check_archive(data, limits)
    try:
        document = WordDocument(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - python-docx raises whatever the damaged part makes it raise
        raise UnsupportedDocumentError("the Word document could not be read") from None

    blocks: list[Block] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = (
                paragraph.style.name if paragraph.style is not None and paragraph.style.name else ""
            )
            level = _heading_level(style)
            if level:
                blocks.append(Block("heading", text, level=level))
            else:
                listed = "list" in style.lower()
                blocks.append(Block("text", f"- {text}" if listed else text))
        elif tag == "tbl":
            table = Table(child, document)
            rows = [
                " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                for row in table.rows
            ]
            body = "\n".join(row for row in rows if row)
            if body:
                blocks.append(Block("table", body))

    with_headings = apply_headings(blocks)
    core_title = (document.core_properties.title or "").strip()
    first_heading = next((b.text for b in with_headings if b.kind == "heading"), None)
    return ParsedDocument(title=core_title or first_heading, blocks=tuple(with_headings))
