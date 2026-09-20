import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.knowledge.chunker import (
    ChunkingConfig,
    Chunks,
    breadcrumb,
    chunk,
    embedding_input,
    max_text_chars,
)
from factotum.knowledge.document import Block, apply_headings
from factotum.knowledge.tokens import estimate_tokens

SMALL = ChunkingConfig(child_tokens=64, parent_tokens=200)
"""A child of about 190 characters and a parent of about 600."""


def text(*sentences: str, page: int | None = None, headings: tuple[str, ...] = ()) -> Block:
    return Block("text", " ".join(sentences), page=page, headings=headings)


def sentences(count: int, word: str = "word") -> list[str]:
    return [f"This is sentence number {n} about the {word}." for n in range(count)]


def words(chunks: Chunks) -> str:
    """All the words of the children, without the white space between them."""
    return "".join("".join(child.text.split()) for child in chunks.children)


class TestConfig:
    def test_the_defaults_are_sensible(self) -> None:
        config = ChunkingConfig()

        assert 128 <= config.child_tokens <= 512
        assert config.parent_tokens >= 2 * config.child_tokens

    def test_a_child_cannot_be_absurdly_small(self) -> None:
        with pytest.raises(ValueError, match="at least"):
            ChunkingConfig(child_tokens=10, parent_tokens=100)

    def test_a_parent_cannot_be_smaller_than_a_child(self) -> None:
        with pytest.raises(ValueError, match="smaller than a child"):
            ChunkingConfig(child_tokens=300, parent_tokens=200)

    def test_it_can_shrink_by_half_until_the_floor(self) -> None:
        config = ChunkingConfig(child_tokens=320, parent_tokens=1200)
        steps = [config]
        while steps[-1].can_shrink:
            steps.append(steps[-1].shrunk())

        assert [step.child_tokens for step in steps] == [320, 160, 80, 64]
        assert all(step.parent_tokens >= step.child_tokens for step in steps)
        assert steps[-1].shrunk() == steps[-1]


class TestBreadcrumb:
    def test_headings_are_one_line_from_the_outermost(self) -> None:
        assert breadcrumb(("Networking", "Routers")) == "Networking > Routers"

    def test_no_headings_no_breadcrumb(self) -> None:
        assert breadcrumb(()) == ""

    def test_when_it_is_too_long_the_outermost_headings_go_first(self) -> None:
        headings = ("A" * 90, "B" * 90, "C" * 90)

        assert breadcrumb(headings) == f"{'B' * 90} > {'C' * 90}"

    def test_one_heading_that_is_too_long_is_cut_from_the_front(self) -> None:
        assert breadcrumb(("x" * 500,)) == "x" * 200

    def test_it_is_put_in_front_of_the_text_that_is_embedded(self) -> None:
        assert embedding_input(("Routers",), "Hold the button.") == "Routers\n\nHold the button."

    def test_without_headings_only_the_text_is_embedded(self) -> None:
        assert embedding_input((), "Hold the button.") == "Hold the button."


class TestSmallDocuments:
    def test_a_short_document_is_one_child_in_one_parent(self) -> None:
        chunks = chunk([text("Hold the reset button.")])

        assert len(chunks.children) == 1
        assert len(chunks.parents) == 1
        assert chunks.children[0].text == "Hold the reset button."
        assert chunks.children[0].parent == 0
        assert chunks.parents[0].text == "Hold the reset button."

    def test_no_blocks_no_chunks(self) -> None:
        assert chunk([]) == Chunks((), ())

    def test_blocks_with_nothing_in_them_are_skipped(self) -> None:
        chunks = chunk([Block("text", ""), text("Real."), Block("code", "  \n")])

        assert [child.text for child in chunks.children] == ["Real."]

    def test_paragraphs_that_fit_together_are_one_child(self) -> None:
        chunks = chunk([text("First."), text("Second.")])

        assert [child.text for child in chunks.children] == ["First.\n\nSecond."]

    def test_it_is_the_same_every_time(self) -> None:
        blocks = [text(*sentences(40))]

        assert chunk(blocks, SMALL) == chunk(blocks, SMALL)


class TestHeadings:
    def blocks(self) -> list[Block]:
        return apply_headings(
            [
                Block("heading", "Resetting", level=1),
                Block("text", "Overview of resetting."),
                Block("heading", "Factory reset", level=2),
                Block("text", "Hold the button for ten seconds."),
                Block("heading", "Passwords", level=1),
                Block("text", "Use the admin page."),
            ]
        )

    def test_a_child_never_crosses_a_heading(self) -> None:
        chunks = chunk(self.blocks())

        assert [(c.headings, c.text) for c in chunks.children] == [
            (("Resetting",), "Overview of resetting."),
            (("Resetting", "Factory reset"), "Hold the button for ten seconds."),
            (("Passwords",), "Use the admin page."),
        ]

    def test_the_headings_are_where_a_piece_is_and_not_part_of_its_text(self) -> None:
        chunks = chunk(self.blocks())

        assert "Factory reset" not in "".join(c.text for c in chunks.children)

    def test_a_parent_is_a_section_and_does_not_cross_a_heading_either(self) -> None:
        chunks = chunk(self.blocks())

        assert [p.headings for p in chunks.parents] == [
            ("Resetting",),
            ("Resetting", "Factory reset"),
            ("Passwords",),
        ]

    def test_a_heading_with_nothing_under_it_makes_no_piece(self) -> None:
        blocks = apply_headings(
            [
                Block("heading", "Part one", level=1),
                Block("heading", "Chapter", level=2),
                Block("text", "Words."),
            ]
        )

        chunks = chunk(blocks)

        assert [(c.headings, c.text) for c in chunks.children] == [
            (("Part one", "Chapter"), "Words.")
        ]

    def test_a_document_of_nothing_but_headings_is_searched_by_them(self) -> None:
        blocks = apply_headings(
            [Block("heading", "Title", level=1), Block("heading", "Sub", level=2)]
        )

        assert [c.text for c in chunk(blocks).children] == ["Title", "Sub"]

    def test_the_same_heading_twice_in_a_row_is_one_section(self) -> None:
        blocks = [text("One.", headings=("A",)), text("Two.", headings=("A",))]

        assert len(chunk(blocks).children) == 1

    def test_the_same_headings_apart_are_two_sections(self) -> None:
        blocks = [
            text("One.", headings=("A",)),
            text("Two.", headings=("B",)),
            text("Three.", headings=("A",)),
        ]

        assert [c.headings for c in chunk(blocks).children] == [("A",), ("B",), ("A",)]


class TestSizes:
    def test_a_long_section_is_many_children_that_all_fit(self) -> None:
        chunks = chunk([text(*sentences(60))], SMALL)

        assert len(chunks.children) > 5
        limit = max_text_chars((), SMALL)
        assert all(len(child.text) <= limit for child in chunks.children)

    def test_the_breadcrumb_counts_against_the_size_of_a_child(self) -> None:
        headings = ("A very long chapter title indeed", "An equally long section title")
        chunks = chunk([text(*sentences(60), headings=headings)], SMALL)

        limit = max_text_chars(headings, SMALL)
        assert limit < max_text_chars((), SMALL)
        assert all(len(c.text) <= limit for c in chunks.children)
        for child in chunks.children:
            assert estimate_tokens(embedding_input(headings, child.text)) <= SMALL.child_tokens + 1

    def test_the_breadcrumb_never_takes_more_than_half(self) -> None:
        headings = ("x" * 190,)

        assert max_text_chars(headings, SMALL) >= int(SMALL.child_tokens * 3.0) // 2

    def test_a_parent_holds_whole_children_up_to_its_size(self) -> None:
        chunks = chunk([text(*sentences(120))], SMALL)

        assert len(chunks.parents) > 1
        for parent in chunks.parents:
            assert len(parent.text) <= 200 * 3
            assert parent.text.count("\n\n") + 1 >= 1
        for child in chunks.children:
            assert child.text in chunks.parents[child.parent].text

    def test_the_children_of_a_parent_are_its_text_in_order(self) -> None:
        chunks = chunk([text(*sentences(120))], SMALL)

        for parent in chunks.parents:
            own = [c.text for c in chunks.children if c.parent == parent.ordinal]
            assert "\n\n".join(own) == parent.text

    def test_ordinals_run_through_the_document(self) -> None:
        chunks = chunk([text(*sentences(120))], SMALL)

        assert [c.ordinal for c in chunks.children] == list(range(len(chunks.children)))
        assert [p.ordinal for p in chunks.parents] == list(range(len(chunks.parents)))
        assert [c.parent for c in chunks.children] == sorted(c.parent for c in chunks.children)

    def test_small_paragraphs_are_packed_together_up_to_the_size_of_a_child(self) -> None:
        blocks = [text(f"Paragraph number {n} says something short.") for n in range(30)]

        chunks = chunk(blocks, SMALL)

        assert 1 < len(chunks.children) < 30
        assert all("\n\n" in child.text for child in chunks.children[:-1])


class TestSplitting:
    def test_a_long_paragraph_is_cut_at_the_end_of_a_sentence(self) -> None:
        chunks = chunk([text(*sentences(30))], SMALL)

        assert len(chunks.children) > 1
        assert all(child.text.endswith(".") for child in chunks.children)
        assert all(child.text.startswith("This is sentence") for child in chunks.children)

    def test_nothing_is_lost_or_repeated_when_a_paragraph_is_cut(self) -> None:
        paragraph = text(*sentences(30))

        assert words(chunk([paragraph], SMALL)) == "".join(paragraph.text.split())

    def test_a_sentence_too_long_for_a_child_is_cut_between_words(self) -> None:
        sentence = " ".join(f"word{n}" for n in range(200)) + "."

        chunks = chunk([text(sentence)], SMALL)

        assert len(chunks.children) > 1
        assert "".join("".join(c.text.split()) for c in chunks.children) == "".join(
            sentence.split()
        )
        assert all(not c.text.startswith("ord") for c in chunks.children)

    def test_a_word_too_long_for_anything_is_cut_anywhere(self) -> None:
        blob = "A" * 1000

        chunks = chunk([text(blob)], SMALL)

        assert "".join(c.text for c in chunks.children) == blob
        assert all(len(c.text) <= max_text_chars((), SMALL) for c in chunks.children)

    def test_questions_and_exclamations_and_closing_quotes_end_sentences_too(self) -> None:
        paragraph = text(*['Is it "done"? Yes! It is (really) done.' for _ in range(20)])

        chunks = chunk([paragraph], SMALL)

        assert len(chunks.children) > 1
        assert all(c.text.endswith((".", "?", "!", '"', ")")) for c in chunks.children)

    def test_text_in_scripts_without_spaces_is_cut_after_its_full_stops(self) -> None:
        paragraph = text("これは文です。" * 60)

        chunks = chunk([paragraph], SMALL)

        assert len(chunks.children) > 1
        assert all(c.text.endswith("。") for c in chunks.children)


class TestCode:
    LISTING = "\n".join(f"    line_{n} = compute({n})" for n in range(60))

    def test_code_is_cut_at_the_end_of_a_line(self) -> None:
        chunks = chunk([Block("code", self.LISTING)], SMALL)

        assert len(chunks.children) > 1
        for child in chunks.children:
            assert all(line.startswith("    line_") for line in child.text.split("\n"))

    def test_indentation_survives(self) -> None:
        chunks = chunk([Block("code", self.LISTING)], SMALL)

        assert all(child.text.startswith("    line_") for child in chunks.children)

    def test_all_the_lines_are_there_in_order(self) -> None:
        chunks = chunk([Block("code", self.LISTING)], SMALL)

        rebuilt = "\n".join(child.text for child in chunks.children)
        assert rebuilt == self.LISTING

    def test_a_line_longer_than_a_child_is_cut(self) -> None:
        chunks = chunk([Block("code", "x = " + "1234567890 " * 80)], SMALL)

        assert len(chunks.children) > 1
        assert all(len(c.text) <= max_text_chars((), SMALL) for c in chunks.children)


class TestTables:
    HEADER = "Model | Button"
    TABLE = HEADER + "\n" + "\n".join(f"Model {n} | left button {n}" for n in range(40))

    def test_a_table_that_fits_is_left_alone(self) -> None:
        chunks = chunk([Block("table", "Model | Button\nA1 | left")], SMALL)

        assert [c.text for c in chunks.children] == ["Model | Button\nA1 | left"]

    def test_a_long_table_is_cut_between_rows_and_each_piece_has_the_header(self) -> None:
        chunks = chunk([Block("table", self.TABLE)], SMALL)

        assert len(chunks.children) > 1
        assert all(c.text.startswith(self.HEADER + "\n") for c in chunks.children)

    def test_every_row_is_in_exactly_one_piece(self) -> None:
        chunks = chunk([Block("table", self.TABLE)], SMALL)

        rows = [line for c in chunks.children for line in c.text.split("\n") if line != self.HEADER]
        assert rows == self.TABLE.split("\n")[1:]

    def test_pieces_of_a_table_fit_with_their_header(self) -> None:
        chunks = chunk([Block("table", self.TABLE)], SMALL)

        assert all(len(c.text) <= max_text_chars((), SMALL) for c in chunks.children)

    def test_a_header_that_is_too_long_to_repeat_is_not(self) -> None:
        header = "Column | " * 12
        table = header + "\n" + "\n".join(f"row {n} | cell {n}" for n in range(40))

        chunks = chunk([Block("table", table)], SMALL)

        assert len(chunks.children) > 1
        assert sum(c.text.startswith(header) for c in chunks.children) == 1


class TestPages:
    def test_a_child_is_on_the_page_where_it_starts(self) -> None:
        blocks = [text(*sentences(4), page=3), text(*sentences(4), page=4)]

        chunks = chunk(blocks, SMALL)

        assert chunks.children[0].page == 3
        assert chunks.parents[0].page == 3

    def test_pages_are_followed_across_children(self) -> None:
        blocks = [text(*sentences(3, word=f"page{p}"), page=p) for p in range(1, 21)]

        chunks = chunk(blocks, SMALL)

        pages = [c.page for c in chunks.children]
        assert pages == sorted(p for p in pages if p is not None)
        assert pages[0] == 1
        assert (pages[-1] or 0) > 10

    def test_a_document_without_pages_has_none(self) -> None:
        assert chunk([text("No pages here.")]).children[0].page is None


blocks_strategy = st.lists(
    st.builds(
        Block,
        kind=st.sampled_from(["text", "code", "heading"]),
        text=st.text(
            alphabet=st.characters(codec="utf-8", exclude_categories=["Cs", "Cc"]), max_size=1500
        ),
        page=st.none() | st.integers(1, 50),
        headings=st.sampled_from([(), ("A",), ("A", "B"), ("Chapter " * 12,)]),
    ),
    max_size=15,
)


class TestProperties:
    @given(blocks_strategy)
    def test_every_child_fits_whatever_the_document(self, blocks: list[Block]) -> None:
        for child in chunk(blocks, SMALL).children:
            assert len(child.text) <= max_text_chars(child.headings, SMALL)

    @given(blocks_strategy)
    def test_no_word_is_lost_or_invented(self, blocks: list[Block]) -> None:
        body = [b for b in blocks if b.kind != "heading"]
        expected = "".join("".join(b.text.split()) for b in body)
        chunks = chunk(blocks, SMALL)

        if expected:
            assert words(chunks) == expected

    @given(blocks_strategy)
    def test_the_structure_is_consistent(self, blocks: list[Block]) -> None:
        chunks = chunk(blocks, SMALL)

        assert [c.ordinal for c in chunks.children] == list(range(len(chunks.children)))
        for child in chunks.children:
            parent = chunks.parents[child.parent]
            assert child.headings == parent.headings
            assert child.text in parent.text
        for parent in chunks.parents:
            assert len(parent.text) <= int(SMALL.parent_tokens * 3.0)
            assert any(c.parent == parent.ordinal for c in chunks.children)

    @given(blocks_strategy)
    def test_it_is_deterministic(self, blocks: list[Block]) -> None:
        assert chunk(blocks, SMALL) == chunk(blocks, SMALL)
