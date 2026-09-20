import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.knowledge.loaders.text import normalize_newlines, parse_markdown, parse_plain


def kinds(parsed_blocks: tuple[object, ...]) -> list[str]:
    return [block.kind for block in parsed_blocks]  # type: ignore[attr-defined]


class TestPlainText:
    def test_paragraphs_are_told_by_blank_lines(self) -> None:
        parsed = parse_plain("First paragraph.\n\nSecond one,\nover two lines.\n\n\n\nThird.")

        assert [b.text for b in parsed.blocks] == [
            "First paragraph.",
            "Second one,\nover two lines.",
            "Third.",
        ]

    def test_windows_and_old_mac_line_endings_are_the_same_as_unix_ones(self) -> None:
        assert parse_plain("a\r\n\r\nb\r\rc").text == parse_plain("a\n\nb\n\nc").text

    def test_indented_paragraphs_are_code(self) -> None:
        parsed = parse_plain("Run this:\n\n    ls -la\n    cd /etc\n\nThen wait.")

        assert kinds(parsed.blocks) == ["text", "code", "text"]
        assert parsed.blocks[1].text == "    ls -la\n    cd /etc"

    def test_a_tab_indent_is_code_too(self) -> None:
        assert parse_plain("\tmake install").blocks[0].kind == "code"

    def test_trailing_spaces_go_and_words_stay(self) -> None:
        assert parse_plain("turn off the firewall   \nthe diff command").blocks[0].text == (
            "turn off the firewall\nthe diff command"
        )

    def test_it_has_no_title_and_no_headings(self) -> None:
        parsed = parse_plain("# not a heading in plain text")

        assert parsed.title is None
        assert parsed.blocks[0].headings == ()

    def test_only_white_space_gives_no_blocks(self) -> None:
        assert parse_plain(" \n\n \t \n").blocks == ()

    def test_bullets_and_newlines_between_paragraphs_survive(self) -> None:
        parsed = parse_plain("Steps:\n- one\n- two\n\nDone.")

        assert parsed.blocks[0].text == "Steps:\n- one\n- two"
        assert len(parsed.blocks) == 2

    @given(st.text(max_size=300))
    def test_no_word_is_lost_or_joined_to_another(self, text: str) -> None:
        # The prototype's cleaner turned "turn off the firewall" into "turn offthe firewall".
        words = normalize_newlines(text).split()

        assert parse_plain(text).text.split() == words


class TestMarkdown:
    SAMPLE = """---
title: Router guide
tags: [network]
---

# Resetting

Hold the **reset** button for [ten seconds](http://example.org).

## Factory reset

- first step
- second step

```bash
sudo reboot
```

| Model | Button |
| --- | --- |
| A1 | left |
| B2 | right |

> A quoted note.

1. one
2. two
"""

    def test_headings_become_blocks_with_their_level_and_the_breadcrumb(self) -> None:
        parsed = parse_markdown(self.SAMPLE)

        headings = [(b.text, b.level) for b in parsed.blocks if b.kind == "heading"]
        assert headings == [("Resetting", 1), ("Factory reset", 2)]
        step = next(b for b in parsed.blocks if b.text == "- first step")
        assert step.headings == ("Resetting", "Factory reset")

    def test_the_title_comes_from_the_front_matter(self) -> None:
        assert parse_markdown(self.SAMPLE).title == "Router guide"

    def test_without_front_matter_the_title_is_the_first_top_level_heading(self) -> None:
        assert parse_markdown("# Only title\n\ntext").title == "Only title"

    def test_without_either_there_is_no_title(self) -> None:
        assert parse_markdown("just text").title is None

    def test_the_front_matter_is_not_part_of_the_text(self) -> None:
        assert "tags" not in parse_markdown(self.SAMPLE).text

    def test_markup_is_gone_and_the_words_stay(self) -> None:
        parsed = parse_markdown(self.SAMPLE)

        assert "Hold the reset button for ten seconds." in parsed.text
        assert "**" not in parsed.text
        assert "http://example.org" not in parsed.text

    def test_list_items_are_kept_as_items(self) -> None:
        parsed = parse_markdown(self.SAMPLE)

        assert "- first step" in [b.text for b in parsed.blocks]
        assert "- two" in [b.text for b in parsed.blocks]

    def test_code_is_kept_as_it_was_written(self) -> None:
        code = [b for b in parse_markdown(self.SAMPLE).blocks if b.kind == "code"]

        assert [b.text for b in code] == ["sudo reboot"]

    def test_a_table_is_a_block_of_rows(self) -> None:
        table = next(b for b in parse_markdown(self.SAMPLE).blocks if b.kind == "table")

        assert table.text == "Model | Button\nA1 | left\nB2 | right"

    def test_a_quote_is_text(self) -> None:
        assert "A quoted note." in [b.text for b in parse_markdown(self.SAMPLE).blocks]

    def test_inline_code_and_images_leave_their_text(self) -> None:
        parsed = parse_markdown("Run `make` then see ![the diagram](d.png).")

        assert parsed.blocks[0].text == "Run make then see the diagram."

    def test_a_hard_break_is_a_line_break_and_a_soft_one_a_space(self) -> None:
        parsed = parse_markdown("one  \ntwo\nthree")

        assert parsed.blocks[0].text == "one\ntwo three"

    def test_html_in_markdown_gives_its_text(self) -> None:
        parsed = parse_markdown("<div>\n<b>inside</b> html\n</div>\n\nafter")

        assert "inside html" in parsed.text
        assert "<b>" not in parsed.text

    def test_empty_headings_and_code_are_dropped(self) -> None:
        assert parse_markdown("#\n\n```\n```\n").blocks == ()

    def test_a_table_without_rows_gives_nothing(self) -> None:
        assert parse_markdown("| a |\n| - |\n").blocks[0].kind == "table"

    @pytest.mark.parametrize("text", ["", "   ", "\n\n"])
    def test_nothing_in_gives_nothing_out(self, text: str) -> None:
        assert parse_markdown(text).blocks == ()

    def test_a_front_matter_that_never_closes_is_just_text(self) -> None:
        assert "title" in parse_markdown("---\ntitle: never closed\n\nhello").text
