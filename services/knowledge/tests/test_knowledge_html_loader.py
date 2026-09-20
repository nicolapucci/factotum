import pytest

from factotum.contracts.errors import UnsupportedDocumentError
from factotum.knowledge.loaders.html import parse_html

PAGE = """<!DOCTYPE html>
<html>
<head>
  <title>Router manual</title>
  <style>body { color: red }</style>
  <script>alert('not text')</script>
</head>
<body>
  <nav>Home | Products | Contact</nav>
  <main>
    <h1>Resetting the router</h1>
    <p>Hold the <b>reset</b> button
       for ten   seconds.<br>Wait for the light.</p>
    <h2>Factory reset</h2>
    <ul><li>First step</li><li>Second step</li></ul>
    <pre>sudo reboot
sudo shutdown -h now</pre>
    <table>
      <tr><th>Model</th><th>Button</th></tr>
      <tr><td>A1</td><td>left</td></tr>
    </table>
    <form><input value="secret"><button>Send</button></form>
    <!-- a comment that is not text -->
    <noscript>Enable JavaScript</noscript>
  </main>
  <footer>Copyright</footer>
</body>
</html>"""


def test_scripts_styles_and_comments_are_not_text() -> None:
    text = parse_html(PAGE).text

    for stray in ("alert", "color: red", "a comment", "Enable JavaScript", "secret", "Send"):
        assert stray not in text


def test_a_page_with_a_main_element_is_read_from_it_and_its_menus_are_left_out() -> None:
    text = parse_html(PAGE).text

    assert "Home | Products" not in text
    assert "Copyright" not in text
    assert "Hold the reset button for ten seconds." in text


def test_the_title_is_the_page_title() -> None:
    assert parse_html(PAGE).title == "Router manual"


def test_without_a_page_title_it_is_the_first_top_level_heading() -> None:
    assert parse_html("<body><h1>Heading</h1><p>text</p></body>").title == "Heading"


def test_headings_carry_their_level_and_the_blocks_below_them_their_breadcrumb() -> None:
    parsed = parse_html(PAGE)

    assert [(b.text, b.level) for b in parsed.blocks if b.kind == "heading"] == [
        ("Resetting the router", 1),
        ("Factory reset", 2),
    ]
    step = next(b for b in parsed.blocks if b.text == "First step")
    assert step.headings == ("Resetting the router", "Factory reset")


def test_white_space_collapses_and_a_br_is_a_line_break() -> None:
    paragraph = next(b for b in parse_html(PAGE).blocks if "reset button" in b.text)

    assert paragraph.text == "Hold the reset button for ten seconds.\nWait for the light."


def test_list_items_are_blocks_of_their_own() -> None:
    texts = [b.text for b in parse_html(PAGE).blocks]

    assert "First step" in texts
    assert "Second step" in texts


def test_preformatted_text_keeps_its_lines() -> None:
    code = next(b for b in parse_html(PAGE).blocks if b.kind == "code")

    assert code.text == "sudo reboot\nsudo shutdown -h now"


def test_a_table_is_a_block_of_rows() -> None:
    table = next(b for b in parse_html(PAGE).blocks if b.kind == "table")

    assert table.text == "Model | Button\nA1 | left"


def test_without_main_or_article_the_whole_body_is_read() -> None:
    parsed = parse_html("<body><nav>menu</nav><p>content</p></body>")

    assert "menu" in parsed.text
    assert "content" in parsed.text


def test_of_several_articles_the_longest_is_the_content() -> None:
    page = "<body><article>short</article><article>a much longer article text</article></body>"

    assert parse_html(page).text == "a much longer article text"


def test_a_fragment_without_html_or_body_is_read() -> None:
    assert parse_html("<p>one</p><p>two</p>").text == "one\n\ntwo"


def test_plain_text_in_a_page_is_read() -> None:
    assert parse_html("just words").text == "just words"


def test_empty_headings_and_code_are_dropped() -> None:
    assert parse_html("<h1> </h1><pre>\n</pre><table></table>").blocks == ()


def test_entities_are_decoded() -> None:
    assert parse_html("<p>Tom &amp; Jerry &lt;3 caf&eacute;</p>").text == "Tom & Jerry <3 café"


def test_a_page_nested_absurdly_deep_is_refused_and_does_not_crash_the_reader() -> None:
    deep = "<div>" * 5000 + "text" + "</div>" * 5000

    with pytest.raises(UnsupportedDocumentError, match="nested too deeply"):
        parse_html(deep)
