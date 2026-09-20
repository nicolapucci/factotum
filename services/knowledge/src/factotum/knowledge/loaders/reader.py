"""From the bytes of a file to a parsed document, with the limits enforced."""

from factotum.contracts.errors import DocumentTooLargeError, UnsupportedDocumentError
from factotum.knowledge.document import Limits, ParsedDocument
from factotum.knowledge.loaders import sniff
from factotum.knowledge.loaders.docx import parse_docx
from factotum.knowledge.loaders.html import parse_html
from factotum.knowledge.loaders.pdf import parse_pdf
from factotum.knowledge.loaders.text import parse_markdown, parse_plain


def parse_document(data: bytes, media_type: str, limits: Limits) -> ParsedDocument:
    """Read a document of a known format. It is never trusted: the limits are checked before and
    after, and what cannot be read is ``UnsupportedDocumentError``, not a crash."""
    if len(data) > limits.max_bytes:
        raise DocumentTooLargeError(
            f"the file is larger than {limits.max_bytes // (1024 * 1024)} MiB"
        )
    match media_type:
        case sniff.PDF:
            parsed = parse_pdf(data, limits)
        case sniff.DOCX:
            parsed = parse_docx(data, limits)
        case sniff.HTML:
            parsed = parse_html(sniff.decode_text(data))
        case sniff.MARKDOWN:
            parsed = parse_markdown(sniff.decode_text(data))
        case sniff.PLAIN:
            parsed = parse_plain(sniff.decode_text(data))
        case _:
            raise UnsupportedDocumentError(f"the format {media_type!r} is not supported")

    total = sum(len(block.text) for block in parsed.blocks)
    if total == 0:
        raise UnsupportedDocumentError("there is no text in the document")
    if total > limits.max_chars:
        raise DocumentTooLargeError(f"the text is longer than {limits.max_chars} characters")
    return parsed
