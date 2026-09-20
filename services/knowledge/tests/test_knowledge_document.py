from factotum.knowledge.document import Block, Limits, ParsedDocument, apply_headings


def heading(text: str, level: int) -> Block:
    return Block("heading", text, level=level)


def text(value: str) -> Block:
    return Block("text", value)


class TestApplyHeadings:
    def test_a_block_knows_the_headings_above_it(self) -> None:
        blocks = apply_headings(
            [heading("Networking", 1), text("intro"), heading("Routers", 2), text("reset it")]
        )

        assert [b.headings for b in blocks] == [
            ("Networking",),
            ("Networking",),
            ("Networking", "Routers"),
            ("Networking", "Routers"),
        ]

    def test_a_heading_of_the_same_level_replaces_the_previous_one(self) -> None:
        blocks = apply_headings([heading("A", 1), heading("B", 1), text("x")])

        assert blocks[-1].headings == ("B",)

    def test_a_shallower_heading_closes_the_deeper_ones(self) -> None:
        blocks = apply_headings(
            [heading("A", 1), heading("A.1", 2), heading("A.1.a", 3), heading("B", 1), text("x")]
        )

        assert blocks[-1].headings == ("B",)

    def test_a_deeper_heading_without_a_parent_stands_alone(self) -> None:
        blocks = apply_headings([heading("Deep", 3), text("x")])

        assert blocks[-1].headings == ("Deep",)

    def test_text_before_the_first_heading_has_none(self) -> None:
        assert apply_headings([text("preface"), heading("A", 1)])[0].headings == ()

    def test_the_kind_page_and_level_are_kept(self) -> None:
        [block] = apply_headings([Block("code", "ls", page=3)])

        assert (block.kind, block.page, block.level) == ("code", 3, 0)

    def test_the_input_is_not_modified(self) -> None:
        original = [heading("A", 1), text("x")]

        apply_headings(original)

        assert original[1].headings == ()


def test_the_text_of_a_document_is_its_blocks_one_after_the_other() -> None:
    parsed = ParsedDocument(title=None, blocks=(text("one"), text("two")))

    assert parsed.text == "one\n\ntwo"


def test_the_defaults_are_generous_but_finite() -> None:
    limits = Limits()

    assert limits.max_bytes == 25 * 1024 * 1024
    assert limits.max_pages > 0
    assert limits.max_chars > 0
