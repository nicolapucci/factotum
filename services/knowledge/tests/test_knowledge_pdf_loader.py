from collections.abc import Callable
from typing import Any

import pytest
from pypdf import PageObject

from factotum.contracts.errors import DocumentTooLargeError, UnsupportedDocumentError
from factotum.knowledge.document import Limits
from factotum.knowledge.loaders import pdf as pdf_module
from factotum.knowledge.loaders.pdf import parse_pdf

PdfFactory = Callable[..., bytes]

MODELS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]

# Every page ends with a sentence of its own: one that came back page after page would be a footer.
CHAPTERS = {
    f"Chapter {number}": [
        f"Chapter {number} explains how the router boots and how to reset it. " * 6,
        f"Hold the reset button of the {model} for ten seconds until the light blinks.",
    ]
    for number, model in enumerate(MODELS, start=1)
}


def test_the_text_of_every_page_is_read(pdf: PdfFactory) -> None:
    parsed = parse_pdf(pdf(CHAPTERS), Limits())

    assert parsed.page_count == 6
    for model in MODELS:
        assert f"Hold the reset button of the {model} for ten seconds" in parsed.text


def test_each_block_knows_its_page(pdf: PdfFactory) -> None:
    parsed = parse_pdf(pdf(CHAPTERS), Limits())

    assert {b.page for b in parsed.blocks} == {1, 2, 3, 4, 5, 6}


def test_the_outline_tells_which_chapter_a_page_is_in(pdf: PdfFactory) -> None:
    parsed = parse_pdf(pdf(CHAPTERS), Limits())

    third = [b for b in parsed.blocks if b.page == 3]
    assert {b.headings for b in third} == {("Chapter 3",)}


def test_nested_bookmarks_make_a_breadcrumb(pdf: PdfFactory) -> None:
    parsed = parse_pdf(pdf(CHAPTERS, sections=True), Limits())

    assert {b.headings for b in parsed.blocks if b.page == 2} == {("Chapter 2", "About Chapter 2")}


def test_the_running_header_and_the_page_numbers_of_a_book_are_not_text(pdf: PdfFactory) -> None:
    parsed = parse_pdf(pdf(CHAPTERS, header="The Book"), Limits())

    assert "The Book:" not in parsed.text
    assert not any(b.text.strip().isdigit() for b in parsed.blocks)


def test_a_line_that_is_not_repeated_is_kept_even_at_the_top(pdf: PdfFactory) -> None:
    once = dict(list(CHAPTERS.items())[:2])

    parsed = parse_pdf(pdf(once, header="Only two pages"), Limits())

    assert "Only two pages: Chapter 1" in parsed.text


def test_a_table_of_contents_with_dot_leaders_is_left_out(pdf: PdfFactory) -> None:
    contents = [
        f"Chapter {n} .......................................... {n * 10}" for n in range(1, 9)
    ]

    parsed = parse_pdf(pdf(CHAPTERS, contents=contents), Limits())

    assert "......" not in parsed.text
    assert parsed.page_count == 7


def test_a_page_that_cannot_be_read_does_not_lose_the_others(
    pdf: PdfFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PageObject.extract_text
    calls: list[int] = []

    def flaky(page: PageObject, *args: Any, **kwargs: Any) -> str:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("a font nobody can decode")
        return original(page, *args, **kwargs)

    monkeypatch.setattr(PageObject, "extract_text", flaky)

    parsed = parse_pdf(pdf(CHAPTERS), Limits())

    assert {b.page for b in parsed.blocks} == {1, 3, 4, 5, 6}


def test_a_pdf_that_has_no_text_layer_is_refused_with_a_reason(
    blank_pdf: Callable[..., bytes],
) -> None:
    with pytest.raises(UnsupportedDocumentError, match="OCR"):
        parse_pdf(blank_pdf(), Limits())


def test_a_pdf_with_too_many_pages_is_refused(pdf: PdfFactory) -> None:
    with pytest.raises(DocumentTooLargeError, match="more than 3 pages"):
        parse_pdf(pdf(CHAPTERS), Limits(max_pages=3))


def test_a_pdf_that_needs_a_password_is_refused(pdf: PdfFactory) -> None:
    with pytest.raises(UnsupportedDocumentError, match="password"):
        parse_pdf(pdf(CHAPTERS, password="secret"), Limits())


def test_damaged_bytes_are_refused_and_do_not_crash(pdf: PdfFactory) -> None:
    with pytest.raises(UnsupportedDocumentError, match="could not be read"):
        parse_pdf(b"%PDF-1.4\nthis is not a pdf at all", Limits())


def test_a_truncated_pdf_is_refused(pdf: PdfFactory) -> None:
    whole = pdf(CHAPTERS)

    with pytest.raises(UnsupportedDocumentError):
        parse_pdf(whole[: len(whole) // 3], Limits())


class TestParagraphs:
    """How the lines of a page become paragraphs. A page has many lines, and the width of a full
    one is judged from all of them: ten full lines, each a sentence, come first."""

    FULL = "x" * 90
    PAGE = tuple(f"{'y' * 90} sentence {n}." for n in range(10))

    def after_the_page(self, *lines: str) -> list[str]:
        return pdf_module._paragraphs([*self.PAGE, *lines])[len(self.PAGE) :]

    def test_lines_that_run_to_the_edge_are_one_paragraph(self) -> None:
        paragraphs = self.after_the_page(f"{self.FULL} a", f"{self.FULL} b", "and it ends here.")

        assert paragraphs == [f"{self.FULL} a {self.FULL} b and it ends here."]

    def test_a_word_split_by_a_hyphen_at_the_end_of_a_line_is_whole_again(self) -> None:
        paragraphs = self.after_the_page(f"{self.FULL} the gun-", "zip command", "Next paragraph.")

        assert paragraphs == [f"{self.FULL} the gunzip command", "Next paragraph."]

    def test_the_spaced_hyphen_some_books_print_is_handled_too(self) -> None:
        paragraphs = self.after_the_page(f"{self.FULL} sub -", "stituted in place.")

        assert paragraphs == [f"{self.FULL} substituted in place."]

    def test_a_hyphen_followed_by_a_capital_is_kept(self) -> None:
        paragraphs = self.after_the_page(f"{self.FULL} the state-", "Of-the-art idea.")

        assert paragraphs == [f"{self.FULL} the state- Of-the-art idea."]

    def test_a_full_line_that_ends_a_sentence_ends_the_paragraph(self) -> None:
        paragraphs = self.after_the_page(
            f"{self.FULL} ends here.", f"{self.FULL} starts anew", "and finishes."
        )

        assert paragraphs == [f"{self.FULL} ends here.", f"{self.FULL} starts anew and finishes."]

    def test_short_lines_that_follow_each_other_stay_together_one_to_a_line(self) -> None:
        paragraphs = self.after_the_page(
            f"{self.FULL} intro:", "$ ls -la", "$ cd /etc", "$ cat hosts", f"{self.FULL} done."
        )

        assert paragraphs == [
            f"{self.FULL} intro:",
            "$ ls -la\n$ cd /etc\n$ cat hosts",
            f"{self.FULL} done.",
        ]

    def test_a_short_line_between_paragraphs_stands_alone(self) -> None:
        paragraphs = self.after_the_page(f"{self.FULL} one.", "A heading", f"{self.FULL} two.")

        assert paragraphs == [f"{self.FULL} one.", "A heading", f"{self.FULL} two."]

    def test_a_page_that_stops_in_the_middle_of_a_sentence_keeps_what_it_has(self) -> None:
        paragraphs = self.after_the_page(f"{self.FULL} and it goes on", f"{self.FULL} and on")

        assert paragraphs == [f"{self.FULL} and it goes on {self.FULL} and on"]

    def test_a_page_of_only_short_lines_is_one_block_of_lines(self) -> None:
        assert pdf_module._paragraphs(["one", "two", "three"]) == ["one\ntwo\nthree"]

    def test_a_single_line_is_a_paragraph(self) -> None:
        assert pdf_module._paragraphs(["Only this."]) == ["Only this."]

    def test_no_lines_no_paragraphs(self) -> None:
        assert pdf_module._paragraphs([]) == []


class TestContentsPage:
    def test_a_page_with_no_lines_is_not_one(self) -> None:
        assert pdf_module._is_contents_page([" ", ""]) is False
        assert pdf_module._is_contents_page([]) is False

    def test_a_page_of_dot_leaders_is_one(self) -> None:
        lines = [f"Chapter {n} ........ {n}" for n in range(5)]

        assert pdf_module._is_contents_page(lines) is True

    def test_prose_that_trails_off_is_not_one(self) -> None:
        lines = ["He waited.... and waited", "Then the light went out", "Nothing happened."]

        assert pdf_module._is_contents_page(lines) is False


class Bookmark:
    def __init__(self, title: str) -> None:
        self.title = title


class FakeReader:
    """What ``_outline`` asks of a pypdf reader: the bookmarks, and where each one points."""

    def __init__(self, bookmarks: list[object], page_of: Callable[[object], int | None]) -> None:
        self.outline = bookmarks
        self._page_of = page_of

    def get_destination_page_number(self, bookmark: object) -> int | None:
        return self._page_of(bookmark)


class DamagedReader:
    @property
    def outline(self) -> object:
        raise RuntimeError("damaged")


class TestOutline:
    def test_a_nested_list_is_a_deeper_level(self) -> None:
        reader = FakeReader(
            [Bookmark("Part"), [Bookmark("Chapter")], Bookmark("Epilogue")],
            lambda bookmark: {"Part": 0, "Chapter": 1, "Epilogue": 3}[bookmark.title],  # type: ignore[attr-defined]
        )

        assert pdf_module._outline(reader, 4) == [  # type: ignore[arg-type]
            ("Part",),
            ("Part", "Chapter"),
            ("Part", "Chapter"),
            ("Epilogue",),
        ]

    def test_a_broken_bookmark_costs_only_itself(self) -> None:
        def page_of(bookmark: object) -> int | None:
            if bookmark.title == "Points nowhere":  # type: ignore[attr-defined]
                raise ValueError("no such page")
            return 1

        reader = FakeReader([Bookmark("Points nowhere"), Bookmark("Fine")], page_of)

        assert pdf_module._outline(reader, 2) == [(), ("Fine",)]  # type: ignore[arg-type]

    def test_a_damaged_outline_costs_the_headings_and_not_the_document(self) -> None:
        assert pdf_module._outline(DamagedReader(), 2) == [(), ()]  # type: ignore[arg-type]

    def test_a_bookmark_beyond_the_last_page_is_ignored(self) -> None:
        reader = FakeReader([Bookmark("Beyond")], lambda _: 99)

        assert pdf_module._outline(reader, 2) == [(), ()]  # type: ignore[arg-type]

    def test_a_bookmark_without_a_page_is_ignored(self) -> None:
        reader = FakeReader([Bookmark("Nowhere")], lambda _: None)

        assert pdf_module._outline(reader, 2) == [(), ()]  # type: ignore[arg-type]

    def test_a_bookmark_without_a_title_is_ignored(self) -> None:
        reader = FakeReader([Bookmark("  ")], lambda _: 0)

        assert pdf_module._outline(reader, 1) == [()]  # type: ignore[arg-type]
