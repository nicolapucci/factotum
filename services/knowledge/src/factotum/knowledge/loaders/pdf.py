"""PDF: the text layer of a page, cleaned of what a printed book puts around it.

A PDF has no paragraphs or headings, only text placed on pages. What it does have, when its author
took the trouble, is an outline (the bookmarks): that gives each page the chapter and section it
belongs to. Scanned pages have no text at all; reading them needs OCR, which is not done here.
"""

import io
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable

from pypdf import PdfReader

from factotum.contracts.errors import DocumentTooLargeError, UnsupportedDocumentError
from factotum.knowledge.document import Block, Limits, ParsedDocument

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LEADERS = re.compile(r"\.{4,}\s*[\divxlcIVXLC]+\s*$")
_DIGITS = re.compile(r"\d+")
_TERMINAL = (".", "?", "!", ":", ";", "”", '"')
_EDGE_LINES = 3
_LEADER_PAGE_SHARE = 0.4
_MIN_TEXT = 20
_MIN_WIDE = 30
_WIDE_SHARE = 0.72
_MIN_REPEATS = 4

# pypdf reports every font it cannot fully decode; the text it extracts is what matters here.
logging.getLogger("pypdf").setLevel(logging.ERROR)


def _clean(text: str) -> str:
    return _CONTROL.sub("", unicodedata.normalize("NFKC", text))


def _shape(line: str) -> str:
    """A line with its numbers blanked: 'Chapter 3: Using the Shell' and '... 14: ...' repeat."""
    return _DIGITS.sub("#", line.strip())


def _running_lines(pages: list[list[str]]) -> tuple[set[str], set[str]]:
    """The lines a book repeats at the top and at the bottom of its pages: chapter names, page
    numbers. A line is one when its shape (numbers blanked) shows up at the same edge on several
    pages."""
    top: Counter[str] = Counter()
    bottom: Counter[str] = Counter()
    for lines in pages:
        top.update({_shape(line) for line in lines[:_EDGE_LINES]})
        bottom.update({_shape(line) for line in lines[-_EDGE_LINES:]})
    return (
        {shape for shape, seen in top.items() if seen >= _MIN_REPEATS},
        {shape for shape, seen in bottom.items() if seen >= _MIN_REPEATS},
    )


def _strip_running(lines: list[str], running: tuple[set[str], set[str]]) -> list[str]:
    def is_running(line: str, shapes: set[str]) -> bool:
        return len(line) <= 90 and _shape(line) in shapes

    top, bottom = running
    head = 0
    while head < min(_EDGE_LINES, len(lines)) and is_running(lines[head], top):
        head += 1
    tail = len(lines)
    while tail > head and tail > len(lines) - _EDGE_LINES and is_running(lines[tail - 1], bottom):
        tail -= 1
    return lines[head:tail]


def _is_contents_page(lines: list[str]) -> bool:
    content = [line for line in lines if line.strip()]
    if not content:
        return False
    leaders = sum(1 for line in content if _LEADERS.search(line))
    return leaders / len(content) >= _LEADER_PAGE_SHARE


def _join(lines: list[str]) -> str:
    """Lines that were one paragraph, as one: a word split by a hyphen at a line end is whole."""
    text = lines[0]
    for line in lines[1:]:
        if text.endswith(("-", " -")) and line[:1].islower():
            text = text.removesuffix(" -").removesuffix("-") + line
        else:
            text = f"{text} {line}"
    return text


def _paragraphs(lines: list[str]) -> list[str]:
    """Paragraphs from the lines of a page. A line as long as the page is wide, that stops in the
    middle of a sentence, goes on in the next; a short line (code, a list, a heading) stands alone,
    and short lines that follow one another stay together, one to a line."""
    lengths = sorted(len(line) for line in lines)
    reference = lengths[int(0.85 * (len(lengths) - 1))] if lengths else 0

    def wide(line: str) -> bool:
        return reference >= _MIN_WIDE and len(line) >= _WIDE_SHARE * reference

    runs: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if not wide(line) or line.endswith(_TERMINAL):
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    paragraphs: list[str] = []
    shorts: list[str] = []
    for run in runs:
        if len(run) == 1 and not wide(run[0]):
            shorts.append(run[0])
            continue
        if shorts:
            paragraphs.append("\n".join(shorts))
            shorts = []
        paragraphs.append(_join(run))
    if shorts:
        paragraphs.append("\n".join(shorts))
    return paragraphs


def _outline(reader: PdfReader, page_count: int) -> list[tuple[str, ...]]:
    """For each page, the chapter and section names in force on it, from the bookmarks."""
    entries: list[tuple[int, int, str]] = []

    def walk(items: Iterable[object], depth: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            try:
                page = reader.get_destination_page_number(item)  # type: ignore[arg-type]
                title = str(getattr(item, "title", "") or "").strip()
            except Exception:  # noqa: BLE001, S112 - a bookmark that points nowhere is skipped
                continue
            if title and page is not None and 0 <= page < page_count:
                entries.append((page, depth, title))

    try:
        walk(reader.outline, 0)
    except Exception:  # noqa: BLE001 - a damaged outline costs the headings, not the document
        entries = []
    entries.sort(key=lambda entry: entry[0])

    stack: list[tuple[int, str]] = []
    headings: list[tuple[str, ...]] = []
    cursor = 0
    for page in range(page_count):
        while cursor < len(entries) and entries[cursor][0] <= page:
            _, depth, title = entries[cursor]
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, title))
            cursor += 1
        headings.append(tuple(title for _, title in stack))
    return headings


def parse_pdf(data: bytes, limits: Limits) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnsupportedDocumentError("the PDF is protected by a password")
        page_count = len(reader.pages)
    except UnsupportedDocumentError:
        raise
    except Exception:  # noqa: BLE001 - pypdf raises whatever a damaged file makes it raise
        raise UnsupportedDocumentError("the PDF could not be read") from None
    if page_count > limits.max_pages:
        raise DocumentTooLargeError(f"the PDF has more than {limits.max_pages} pages")

    pages: list[list[str]] = []
    for page in reader.pages:
        try:
            text = _clean(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one page that cannot be read does not lose the others
            text = ""
        pages.append([line.strip() for line in text.split("\n") if line.strip()])

    running = _running_lines(pages)
    headings = _outline(reader, page_count)
    blocks: list[Block] = []
    for number, lines in enumerate(pages):
        lines = _strip_running(lines, running)
        if not lines or _is_contents_page(lines):
            continue
        blocks.extend(
            Block("text", paragraph, page=number + 1, headings=headings[number])
            for paragraph in _paragraphs(lines)
            if paragraph.strip()
        )

    if sum(len(block.text) for block in blocks) < _MIN_TEXT:
        raise UnsupportedDocumentError(
            "the PDF has no text in it: it may be scanned pages, which need OCR (not supported yet)"
        )
    title = (
        str(reader.metadata.title).strip() if reader.metadata and reader.metadata.title else None
    )
    return ParsedDocument(title=title or None, blocks=tuple(blocks), page_count=page_count)
