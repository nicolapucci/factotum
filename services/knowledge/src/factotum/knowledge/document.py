"""What reading a document produces: blocks of text with their place in it.

A *block* is a paragraph, a heading, a piece of code or a table: the unit the loaders agree on and
the chunker packs. Each block knows the headings above it, so a passage found later can say where in
the document it is.
"""

from dataclasses import dataclass
from typing import Literal

BlockKind = Literal["heading", "text", "code", "table"]


@dataclass(frozen=True, slots=True)
class Block:
    kind: BlockKind
    text: str
    page: int | None = None
    headings: tuple[str, ...] = ()
    """The headings above the block, from the outermost: ``("Networking", "Routers")``."""

    level: int = 0
    """For a heading, how deep it is (1 is the outermost)."""


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    title: str | None
    blocks: tuple[Block, ...]
    page_count: int | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks)


@dataclass(frozen=True, slots=True)
class Limits:
    """What a document may cost to read. A hostile file must not take the service down."""

    max_bytes: int = 25 * 1024 * 1024
    max_pages: int = 2000
    max_chars: int = 5_000_000
    max_zip_bytes: int = 200 * 1024 * 1024
    """For formats that are zip archives (DOCX): the most they may expand to."""


def apply_headings(blocks: list[Block]) -> list[Block]:
    """The blocks, each with the headings above it, from the heading blocks among them."""
    stack: list[tuple[int, str]] = []
    result: list[Block] = []
    for block in blocks:
        if block.kind == "heading":
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text))
        result.append(
            Block(
                block.kind,
                block.text,
                block.page,
                tuple(text for _, text in stack),
                block.level,
            )
        )
    return result
