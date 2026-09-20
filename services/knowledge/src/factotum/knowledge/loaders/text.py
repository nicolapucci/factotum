"""Plain text and Markdown."""

import re

from markdown_it import MarkdownIt
from markdown_it.token import Token

from factotum.knowledge.document import Block, ParsedDocument, apply_headings

_BLANK_LINES = re.compile(r"\n[ \t]*\n")
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)
_TITLE_LINE = re.compile(r"^title:[ \t]*(.+)$", re.MULTILINE | re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t]+")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_plain(text: str) -> ParsedDocument:
    """Paragraphs separated by blank lines. A paragraph indented by four spaces or a tab is code."""
    blocks: list[Block] = []
    for paragraph in _BLANK_LINES.split(normalize_newlines(text)):
        lines = [line.rstrip() for line in paragraph.strip("\n").split("\n")]
        body = "\n".join(lines).strip("\n")
        if not body.strip():
            continue
        indented = all(line.startswith(("    ", "\t")) for line in lines if line.strip())
        blocks.append(Block("code" if indented else "text", body.strip() if not indented else body))
    return ParsedDocument(title=None, blocks=tuple(blocks))


def _inline_text(inline: Token) -> str:
    parts: list[str] = []
    for child in inline.children or []:
        match child.type:
            case "text" | "code_inline":
                parts.append(child.content)
            case "image":
                parts.append(child.content)
            case "softbreak":
                parts.append(" ")
            case "hardbreak":
                parts.append("\n")
    return _SPACES.sub(" ", "".join(parts)).strip()


def _front_matter(text: str) -> tuple[str, str | None]:
    matched = _FRONT_MATTER.match(text)
    if matched is None:
        return text, None
    title = _TITLE_LINE.search(matched.group(1))
    return text[matched.end() :], title.group(1).strip().strip("\"'") if title else None


def parse_markdown(text: str) -> ParsedDocument:
    body, front_title = _front_matter(normalize_newlines(text))
    tokens = MarkdownIt("commonmark").enable("table").parse(body)

    blocks: list[Block] = []
    lists = 0
    row: list[str] = []
    rows: list[str] = []
    in_table = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        match token.type:
            case "heading_open":
                heading = _inline_text(tokens[index + 1])
                if heading:
                    blocks.append(Block("heading", heading, level=int(token.tag[1])))
                index += 2
            case "paragraph_open":
                paragraph = _inline_text(tokens[index + 1])
                if paragraph:
                    blocks.append(Block("text", f"- {paragraph}" if lists else paragraph))
                index += 2
            case "bullet_list_open" | "ordered_list_open":
                lists += 1
            case "bullet_list_close" | "ordered_list_close":
                lists -= 1
            case "fence" | "code_block":
                code = token.content.rstrip("\n")
                if code.strip():
                    blocks.append(Block("code", code))
            case "html_block":
                stripped = _SPACES.sub(" ", _TAGS.sub(" ", token.content)).strip()
                if stripped:
                    blocks.append(Block("text", stripped))
            case "table_open":
                in_table, rows = True, []
            case "tr_open":
                row = []
            case "inline" if in_table:
                row.append(_inline_text(token))
            case "tr_close":
                cells = [cell for cell in row if cell]
                if cells:
                    rows.append(" | ".join(cells))
            case "table_close":
                in_table = False
                if rows:
                    blocks.append(Block("table", "\n".join(rows)))
        index += 1

    with_headings = apply_headings(blocks)
    title = front_title or next(
        (b.text for b in with_headings if b.kind == "heading" and b.level == 1), None
    )
    return ParsedDocument(title=title, blocks=tuple(with_headings))
