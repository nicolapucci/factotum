"""How a text is cut into what a voice says one at a time, and what a voice leaves out of it."""

from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from factotum.common.sentences import (
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    SentenceChunker,
    sentences_of,
    speakable,
)


def chunks_of(*pieces: str) -> list[str]:
    chunker = SentenceChunker()
    found = [chunk for piece in pieces for chunk in chunker.feed(piece)]
    return found + chunker.flush()


class TestCuttingTextIntoSentences:
    def test_a_sentence_is_sent_when_it_is_whole_and_not_before(self) -> None:
        chunker = SentenceChunker()

        assert chunker.feed("Hold the reset button for ten seconds") == []
        assert chunker.feed(".") == []  # a full stop is an end only when a space follows it
        assert chunker.feed(" ") == []  # and it is the end when something else follows
        assert chunker.feed("Then") == ["Hold the reset button for ten seconds."]

    def test_a_full_stop_inside_a_number_is_not_the_end_of_a_sentence(self) -> None:
        chunker = SentenceChunker()

        assert chunker.feed("The router runs at 3.5 gigahertz. Then") == [
            "The router runs at 3.5 gigahertz."
        ]
        assert chunker.flush() == ["Then"]

    def test_the_marker_of_a_citation_after_the_full_stop_stays_with_its_sentence(self) -> None:
        assert chunks_of("Hold the reset button for ten seconds. [1] Then release it.") == [
            "Hold the reset button for ten seconds. [1]",
            "Then release it.",
        ]
        assert chunks_of("Hold the reset button for ten seconds. [1, 3]  Then release it.") == [
            "Hold the reset button for ten seconds. [1, 3]",
            "Then release it.",
        ]

    def test_a_marker_that_has_only_begun_is_waited_for(self) -> None:
        chunker = SentenceChunker()

        assert chunker.feed("Hold the reset button for ten seconds. ") == []
        assert chunker.feed("[1") == []
        assert chunker.feed(", 3") == []
        assert chunker.feed("]") == ["Hold the reset button for ten seconds. [1, 3]"]

    def test_something_in_brackets_that_is_not_a_citation_begins_the_next_sentence(self) -> None:
        assert chunks_of("Hold the reset button for ten seconds. [note] Then release it.") == [
            "Hold the reset button for ten seconds.",
            "[note] Then release it.",
        ]

    def test_a_marker_at_the_very_end_belongs_to_the_last_sentence(self) -> None:
        chunker = SentenceChunker()

        assert chunker.feed("Hold the reset button for ten seconds. [1]") == [
            "Hold the reset button for ten seconds. [1]"
        ]
        assert chunker.flush() == []

    def test_a_sentence_can_end_in_a_question_mark_or_be_quoted(self) -> None:
        assert chunks_of('Did you say "reset the router"? Yes, that is what I heard. ') == [
            'Did you say "reset the router"?',
            "Yes, that is what I heard.",
        ]

    def test_short_sentences_are_said_with_the_one_after_them(self) -> None:
        assert chunks_of("Yes. The button must be held for ten seconds. ") == [
            "Yes. The button must be held for ten seconds."
        ]

    def test_a_sentence_that_is_long_enough_stands_alone(self) -> None:
        first = "x" * MIN_CHUNK_CHARS + "."

        assert chunks_of(f"{first} Ok. Then this one is long enough by itself. ") == [
            first,
            "Ok. Then this one is long enough by itself.",
        ]

    def test_a_line_is_the_end_of_a_sentence(self) -> None:
        assert chunks_of(
            "Mix the flour with the water\nLet the dough rest overnight in the fridge\n"
        ) == [
            "Mix the flour with the water",
            "Let the dough rest overnight in the fridge",
        ]

    def test_what_is_left_when_nothing_more_comes_is_the_last_sentence(self) -> None:
        chunker = SentenceChunker()
        chunker.feed("Hold the button until the light blinks")

        assert chunker.flush() == ["Hold the button until the light blinks"]
        assert chunker.flush() == []

    def test_a_long_stretch_with_no_end_is_cut_at_a_comma_or_a_space(self) -> None:
        words = ("word " * 120).strip()

        found = chunks_of(words)

        assert len(found) > 1
        assert all(len(chunk) <= MAX_CHUNK_CHARS for chunk in found)
        assert all(chunk.split() == ["word"] * len(chunk.split()) for chunk in found)  # no word cut

    def test_it_prefers_to_cut_after_a_comma(self) -> None:
        text = "one, " * 30 + "two " * 40

        first = chunks_of(text)[0]

        assert first.endswith(",")

    def test_a_word_longer_than_a_chunk_is_cut_because_it_has_to_be(self) -> None:
        found = chunks_of("x" * (MAX_CHUNK_CHARS * 2 + 10))

        assert [len(chunk) for chunk in found] == [MAX_CHUNK_CHARS, MAX_CHUNK_CHARS, 10]

    def test_nothing_is_sent_for_nothing(self) -> None:
        assert chunks_of("", "  ", "\n\n") == []

    @given(
        text=st.text(alphabet="ab .!?,\n[]12", max_size=1500).filter(
            lambda t: all(len(w) < 250 for w in t.split())
        ),
        cuts=st.lists(st.integers(min_value=0, max_value=1500), max_size=12),
    )
    def test_the_sentences_are_the_same_however_the_text_arrived(
        self, text: str, cuts: list[int]
    ) -> None:
        points = sorted({min(cut, len(text)) for cut in cuts} | {0, len(text)})
        pieces = [text[a:b] for a, b in pairwise(points)]

        assert chunks_of(*pieces) == chunks_of(text)

    @given(
        text=st.text(alphabet="ab .!?,\n[]12", max_size=1500).filter(
            lambda t: all(len(w) < 250 for w in t.split())
        )
    )
    def test_nothing_is_lost_repeated_or_too_long_and_no_sentence_is_empty(self, text: str) -> None:
        found = chunks_of(text)

        assert " ".join(" ".join(found).split()) == " ".join(text.split())
        assert all(
            chunk and chunk == chunk.strip() and len(chunk) <= MAX_CHUNK_CHARS for chunk in found
        )


class TestWhatAVoiceShouldSay:
    def test_the_marks_of_a_citation_are_for_the_eyes(self) -> None:
        assert (
            speakable("Hold the button for ten seconds [1].") == "Hold the button for ten seconds."
        )
        assert speakable("It is erased [1, 3]. Then reset.") == "It is erased. Then reset."
        assert speakable("[2]") == ""

    def test_the_marks_of_markdown_are_not_said(self) -> None:
        assert (
            speakable("**Important:** hold the `reset` button")
            == "Important: hold the reset button"
        )
        assert speakable("# Factory reset") == "Factory reset"
        assert speakable("- Mix the flour") == "Mix the flour"
        assert speakable("2. Add the water") == "Add the water"

    def test_a_link_is_said_by_its_words_and_not_by_its_address(self) -> None:
        assert speakable("See [the manual](https://example.org/manual.pdf) for more") == (
            "See the manual for more"
        )

    def test_an_underscore_that_is_part_of_a_word_stays(self) -> None:
        assert speakable("the set_led command and _this_ one") == "the set_led command and this one"

    def test_space_is_one_space(self) -> None:
        assert speakable("  a \n b\t c  ") == "a b c"


class TestWhatAVoiceIsToSayOfAWholeText:
    def test_it_is_the_sentences_of_the_text_in_order(self) -> None:
        text = "Dinner is ready. Come to the table, everybody, because it is getting cold. Now."

        assert sentences_of(text) == [
            "Dinner is ready. Come to the table, everybody, because it is getting cold.",
            "Now.",
        ]

    def test_what_is_for_the_eyes_is_left_out_of_each(self) -> None:
        text = "## Pasta\n\n- Boil **plenty** of water, with salt [1].\n- Add the pasta, see [the box](https://x.example)."

        assert sentences_of(text) == [
            "Pasta Boil plenty of water, with salt.",  # a heading is too short to be said alone
            "Add the pasta, see the box.",
        ]

    def test_a_text_that_is_marks_has_nothing_to_say(self) -> None:
        assert sentences_of("") == []
        assert sentences_of("  \n ") == []
        assert sentences_of("[1]") == []
        assert sentences_of("**") == []

    def test_a_longer_limit_makes_longer_sentences_and_a_shorter_one_shorter(self) -> None:
        text = ("word " * 200).strip() + "."

        longer = sentences_of(text, max_chars=400)
        shorter = sentences_of(text, max_chars=100)

        assert max(map(len, longer)) <= 400
        assert max(map(len, shorter)) <= 100
        assert len(shorter) > len(longer)
        assert " ".join(longer) == text  # nothing is lost, and nothing is said twice

    @given(st.text(max_size=600), st.integers(min_value=30, max_value=400))
    def test_nothing_is_longer_than_the_limit_and_nothing_is_empty(
        self, text: str, limit: int
    ) -> None:
        said = sentences_of(text, max_chars=limit)

        assert all(sentence.strip() and len(sentence) <= limit for sentence in said)
