"""HTML: the words of a page, without its scripts, its menus and its markup."""

import re

from bs4 import BeautifulSoup
from bs4.element import Comment, Doctype, NavigableString, PreformattedString, Tag

from factotum.contracts.errors import UnsupportedDocumentError
from factotum.knowledge.document import Block, ParsedDocument, apply_headings

_SKIP = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
        "form", "button", "select", "textarea", "option", "head",
    }
)  # fmt: skip
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCKS = frozenset(
    {
        "p", "div", "section", "article", "li", "ul", "ol", "blockquote", "dd", "dt", "dl",
        "figure", "figcaption", "header", "footer", "main", "aside", "nav", "address", "details",
        "summary", "fieldset", "hr", "body", "html", "center",
    }
)  # fmt: skip
_BREAK = ""
_WHITESPACE = re.compile(r"\s+")


def _collapse(text: str) -> str:
    """Runs of white space become one space; a ``<br>`` stays a line break."""
    collapsed = _WHITESPACE.sub(" ", text).strip()
    return re.sub(r" ?" + _BREAK + r" ?", "\n", collapsed).strip()


class _Walker:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self._buffer: list[str] = []

    def flush(self) -> None:
        text = _collapse("".join(self._buffer))
        self._buffer.clear()
        if text:
            self.blocks.append(Block("text", text))

    def _table(self, table: Tag) -> None:
        rows: list[str] = []
        for tr in table.find_all("tr"):
            cells = [_collapse(cell.get_text(" ")) for cell in tr.find_all(["td", "th"])]
            row = " | ".join(cell for cell in cells if cell)
            if row:
                rows.append(row)
        if rows:
            self.blocks.append(Block("table", "\n".join(rows)))

    def visit(self, node: object) -> None:
        if isinstance(node, NavigableString):
            if not isinstance(node, (PreformattedString, Comment, Doctype)):
                self._buffer.append(str(node))
            return
        if not isinstance(node, Tag) or node.name in _SKIP:
            return
        name = node.name
        if name in _HEADINGS:
            self.flush()
            heading = _collapse(node.get_text(" "))
            if heading:
                self.blocks.append(Block("heading", heading, level=_HEADINGS[name]))
        elif name == "pre":
            self.flush()
            code = node.get_text().strip("\n")
            if code.strip():
                self.blocks.append(Block("code", code))
        elif name == "table":
            self.flush()
            self._table(node)
        elif name == "br":
            self._buffer.append(_BREAK)
        else:
            block = name in _BLOCKS
            if block:
                self.flush()
            for child in node.children:
                self.visit(child)
            if block:
                self.flush()


def parse_html(text: str) -> ParsedDocument:
    soup = BeautifulSoup(text, "html.parser")
    # The main content of the page when it says which it is; the whole body otherwise.
    mains = soup.find_all(["main", "article"])
    root = max(mains, key=lambda tag: len(tag.get_text())) if mains else (soup.body or soup)

    walker = _Walker()
    try:
        walker.visit(root)
    except RecursionError:
        raise UnsupportedDocumentError("the page is nested too deeply to be read") from None
    walker.flush()

    blocks = apply_headings(walker.blocks)
    page_title = _collapse(soup.title.get_text(" ")) if soup.title else ""
    first_h1 = next((b.text for b in blocks if b.kind == "heading" and b.level == 1), None)
    return ParsedDocument(title=page_title or first_h1, blocks=tuple(blocks))
