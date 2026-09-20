"""Cutting a document into the pieces that are searched and the sections that explain them.

A search wants small pieces: a vector stands for one idea, and a question is answered by a
paragraph, not by a chapter. A reader wants the paragraph in its place. So a document is cut twice.
The **children** are what is embedded and matched; each belongs to a **parent**, the section around
it, which is what comes back as context.

Both are packed from the blocks the loaders produced and never cross a heading: a child is always
under one breadcrumb (``Networking > Routers``), which is put in front of its text when it is
embedded, so that "reset" under "Routers" is not the same as "reset" under "Passwords". Headings are
not part of the text of a piece, only of where it is.

Sizes are in estimated tokens (``tokens``). A block bigger than a piece is cut at the end of a
sentence, else between words, else anywhere; code and tables at the end of a line, and a table
repeats its header row in every piece.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from factotum.knowledge.document import Block
from factotum.knowledge.tokens import CHARS_PER_TOKEN

_BREADCRUMB_SEPARATOR = " > "
_BREADCRUMB_MAX_CHARS = 200
_PARAGRAPH = "\n\n"
# The end of a sentence: a full stop, closing quotes and brackets, and a space. CJK text has no
# spaces between sentences, so its full stops (full-width on purpose) end one by themselves.
_SENTENCE_END = re.compile(
    r"[.!?…]+[\"'”’)\]]*(?:\s+|$)"  # noqa: RUF001
    r"|[。！？]+[\"'”’)\]」』）]*\s*"  # noqa: RUF001
)
_WORD = re.compile(r"\S+\s*")
_LINE = re.compile(r"[^\n]*\n|[^\n]+$")
_MIN_CHILD_TOKENS = 64


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    child_tokens: int = 320
    """The most a child may take (breadcrumb included), in estimated tokens."""

    parent_tokens: int = 1200
    """The most a parent may take, in estimated tokens."""

    def __post_init__(self) -> None:
        if self.child_tokens < _MIN_CHILD_TOKENS:
            raise ValueError(f"a child must be allowed at least {_MIN_CHILD_TOKENS} tokens")
        if self.parent_tokens < self.child_tokens:
            raise ValueError("a parent cannot be smaller than a child")

    @property
    def can_shrink(self) -> bool:
        return self.child_tokens > _MIN_CHILD_TOKENS

    def shrunk(self) -> "ChunkingConfig":
        """Half the size, for when the embedding model says the pieces do not fit."""
        if not self.can_shrink:
            return self
        child = max(_MIN_CHILD_TOKENS, self.child_tokens // 2)
        return ChunkingConfig(child, max(child, self.parent_tokens // 2))


@dataclass(frozen=True, slots=True)
class Parent:
    ordinal: int
    text: str
    headings: tuple[str, ...]
    page: int | None


@dataclass(frozen=True, slots=True)
class Child:
    ordinal: int
    parent: int
    """The ordinal of the parent it belongs to."""

    text: str
    headings: tuple[str, ...]
    page: int | None
    """The page it starts on, when the document has pages."""


@dataclass(frozen=True, slots=True)
class Chunks:
    parents: tuple[Parent, ...]
    children: tuple[Child, ...]


@dataclass(frozen=True, slots=True)
class _Unit:
    text: str
    page: int | None


def breadcrumb(headings: tuple[str, ...]) -> str:
    """Where in the document, as one line: ``Networking > Routers``. When it is too long the
    outermost headings go first, since the innermost say the most."""
    kept = list(headings)
    while len(kept) > 1 and len(_BREADCRUMB_SEPARATOR.join(kept)) > _BREADCRUMB_MAX_CHARS:
        kept.pop(0)
    line = _BREADCRUMB_SEPARATOR.join(kept)
    return line if len(line) <= _BREADCRUMB_MAX_CHARS else line[-_BREADCRUMB_MAX_CHARS:]


def embedding_input(headings: tuple[str, ...], text: str) -> str:
    """What is sent to the embedding model for a piece: its place, then its words."""
    crumb = breadcrumb(headings)
    return f"{crumb}{_PARAGRAPH}{text}" if crumb else text


def max_text_chars(headings: tuple[str, ...], config: ChunkingConfig) -> int:
    """The longest text a child under these headings may have, so that with its breadcrumb it stays
    within the size of a child."""
    total = int(config.child_tokens * CHARS_PER_TOKEN)
    crumb = breadcrumb(headings)
    room = total - len(crumb) - len(_PARAGRAPH) if crumb else total
    return max(room, total // 2)


def _sentences(text: str) -> list[str]:
    pieces: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        pieces.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        pieces.append(text[start:])
    return pieces


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _lines(text: str) -> list[str]:
    return _LINE.findall(text)


def _split(
    text: str,
    limit: int,
    splitters: tuple[Callable[[str], list[str]], ...],
    tidy: Callable[[str], str],
) -> list[str]:
    """Pieces of ``text`` of at most ``limit`` characters. It is cut where the first splitter cuts,
    and where that is not fine enough, where the next does; last of all, anywhere."""
    text = tidy(text)
    if len(text) <= limit:
        return [text] if text else []
    if not splitters:
        return [text[start : start + limit] for start in range(0, len(text), limit)]

    finer = splitters[1:]
    segments: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if tidy(current):
            segments.append(tidy(current))
        current = ""

    for piece in splitters[0](text):
        if len(tidy(piece)) > limit:
            flush()
            segments.extend(_split(piece, limit, finer, tidy))
        elif current and len(tidy(current + piece)) > limit:
            flush()
            current = piece
        else:
            current += piece
    flush()
    return segments


def _prose(text: str) -> str:
    return text.strip()


def _preformatted(text: str) -> str:
    """Indentation is meaning in code: only the empty lines around it go."""
    return text.strip("\r\n").rstrip()


def _pieces(block: Block, limit: int) -> list[str]:
    text = block.text
    if block.kind == "code":
        return _split(text, limit, (_lines, _words), _preformatted)
    if block.kind == "table":
        header = text.partition("\n")[0]
        pieces = _split(text, limit, (_lines, _words), _prose)
        if len(pieces) > 1 and len(header) < limit // 3:
            room = limit - len(header) - 1
            pieces = _split(text, room, (_lines, _words), _prose)
            return [pieces[0], *(f"{header}\n{piece}" for piece in pieces[1:])]
        return pieces
    return _split(text, limit, (_sentences, _words), _prose)


def _sections(blocks: Iterable[Block]) -> list[list[Block]]:
    """Runs of blocks under the same headings."""
    sections: list[list[Block]] = []
    for block in blocks:
        if not block.text.strip():
            continue
        if sections and sections[-1][0].headings == block.headings:
            sections[-1].append(block)
        else:
            sections.append([block])
    return sections


def chunk(blocks: Iterable[Block], config: ChunkingConfig | None = None) -> Chunks:
    """Cut a document into parents and children, in the order of the document."""
    config = config or ChunkingConfig()
    parent_chars = int(config.parent_tokens * CHARS_PER_TOKEN)
    sections = _sections(blocks)
    # A document of headings and nothing else (a title page, a menu) is searched by its headings.
    only_headings = all(b.kind == "heading" for section in sections for b in section)

    parents: list[Parent] = []
    children: list[Child] = []
    for section in sections:
        headings = section[0].headings
        limit = max_text_chars(headings, config)
        units = [
            _Unit(piece, block.page)
            for block in section
            if only_headings or block.kind != "heading"
            for piece in _pieces(block, limit)
        ]

        packed: list[list[_Unit]] = []
        for unit in units:
            if (
                packed
                and sum(len(u.text) + len(_PARAGRAPH) for u in packed[-1]) + len(unit.text) <= limit
            ):
                packed[-1].append(unit)
            else:
                packed.append([unit])

        family: list[list[_Unit]] = []
        size = 0
        for child in packed:
            child_chars = sum(len(u.text) for u in child) + len(_PARAGRAPH) * (len(child) - 1)
            if family and size + len(_PARAGRAPH) + child_chars > parent_chars:
                _emit(parents, children, headings, family)
                family, size = [], 0
            size += child_chars + (len(_PARAGRAPH) if family else 0)
            family.append(child)
        if family:
            _emit(parents, children, headings, family)
    return Chunks(tuple(parents), tuple(children))


def _emit(
    parents: list[Parent],
    children: list[Child],
    headings: tuple[str, ...],
    family: list[list[_Unit]],
) -> None:
    texts = [_PARAGRAPH.join(u.text for u in child) for child in family]
    parent, first = len(parents), len(children)
    parents.append(Parent(parent, _PARAGRAPH.join(texts), headings, family[0][0].page))
    children.extend(
        Child(first + offset, parent, text, headings, child[0].page)
        for offset, (text, child) in enumerate(zip(texts, family, strict=True))
    )
