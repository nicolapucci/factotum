from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from factotum.contracts.capabilities import Capability
from factotum.contracts.conversation import (
    Cited,
    Delta,
    Failed,
    Finished,
    Phase,
    ReferenceKind,
    Status,
    TurnEvent,
)
from factotum.contracts.response import (
    CardPart,
    ResponsePart,
    Speech,
    StatusPart,
    StatusState,
    Text,
)
from factotum.edge.parts import (
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    SentenceChunker,
    TurnParts,
    sources_card,
    speakable,
)
from factotum.edge.testing import answer_events, citation

VOICE = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT})
SCREEN = frozenset({Capability.DISPLAY})
BOTH = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.DISPLAY})
BUTTON = frozenset({Capability.BUTTONS, Capability.LED})


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


class TestTheSourcesOfAnAnswer:
    def test_they_are_a_card_of_the_documents_with_where_in_each(self) -> None:
        card = sources_card([citation(1, "Router manual", 12), citation(2, "Warranty", None)])

        assert card.title == "Sources"
        assert [(i.label, i.value) for i in card.items] == [
            ("[1] Router manual", "page 12 · Router > Factory reset"),
            ("[2] Warranty", "Router > Factory reset"),
        ]

    def test_a_document_with_no_page_and_no_headings_has_nothing_to_say_of_where(self) -> None:
        bare = citation(1, "Notes", None).model_copy(update={"headings": ()})

        assert sources_card([bare]).items[0].value is None

    def test_a_long_title_is_cut_to_fit_and_there_are_at_most_twenty(self) -> None:
        card = sources_card([citation(n, "T" * 300) for n in range(1, 30)])

        assert len(card.items) == 20
        assert all(len(item.label) <= 100 for item in card.items)


def parts_of(capabilities: frozenset[Capability], *events: TurnEvent) -> list[ResponsePart]:
    turn = TurnParts(capabilities)
    found: list[ResponsePart] = []
    for event in events:
        found += turn.feed(event)
    return found


def sources(*numbers: int) -> Cited:
    return Cited(citations=tuple(citation(n) for n in numbers))


class TestWhatEachKindOfDeviceIsSent:
    ANSWER = ("Hold the reset button for ten seconds [1]. ", "All settings are erased [1].")

    def events(self) -> list[TurnEvent]:
        return answer_events(*self.ANSWER, sources=[citation(1)])

    def test_a_speaker_is_sent_what_it_can_say_and_nothing_it_can_only_show(self) -> None:
        parts = parts_of(VOICE, *self.events())

        assert [type(p) for p in parts] == [StatusPart, Speech, Speech]
        assert [p.text for p in parts if isinstance(p, Speech)] == [
            "Hold the reset button for ten seconds.",
            "All settings are erased.",
        ]

    def test_a_screen_is_sent_the_text_with_its_citations_and_the_sources_as_a_card(self) -> None:
        parts = parts_of(SCREEN, *self.events())

        assert [type(p) for p in parts] == [StatusPart, Text, Text, CardPart]
        assert [p.text for p in parts if isinstance(p, Text)] == [
            "Hold the reset button for ten seconds [1].",
            "All settings are erased [1].",
        ]

    def test_a_device_with_both_is_sent_each_sentence_to_say_and_to_read(self) -> None:
        parts = parts_of(BOTH, *self.events())

        assert [type(p) for p in parts] == [StatusPart, Speech, Text, Speech, Text, CardPart]

    def test_a_device_that_can_neither_say_nor_show_is_sent_only_hints(self) -> None:
        parts = parts_of(BUTTON, *self.events())

        assert [type(p) for p in parts] == [StatusPart]

    def test_no_part_is_sent_to_a_device_that_cannot_render_it(self) -> None:
        for capabilities in (VOICE, SCREEN, BOTH, BUTTON):
            parts = parts_of(capabilities, *self.events(), *answer_events("Another one. "))
            for part in parts:
                if isinstance(part, Speech):
                    assert Capability.AUDIO_OUT in capabilities
                if isinstance(part, Text | CardPart):
                    assert Capability.DISPLAY in capabilities


class TestAsTheAnswerIsWritten:
    def test_a_sentence_is_sent_the_moment_it_is_whole_and_not_a_word_of_the_next_before(
        self,
    ) -> None:
        turn = TurnParts(VOICE)

        assert turn.feed(Delta(text="Hold the reset button for ten ")) == []
        assert turn.feed(Delta(text="seconds. Then")) == [
            Speech(text="Hold the reset button for ten seconds.")
        ]
        assert turn.feed(Delta(text=" release it and wait for the light.")) == []
        assert turn.feed(
            Finished(
                reference=ReferenceKind.NONE,
                exchanges_used=0,
                passages_used=0,
                untrusted_content=False,
            )
        ) == [Speech(text="Then release it and wait for the light.")]

    def test_what_is_left_is_said_before_the_sources_are_shown(self) -> None:
        turn = TurnParts(BOTH)
        turn.feed(Delta(text="It is erased [1]"))

        parts = turn.feed(sources(1))

        assert [type(p) for p in parts] == [Speech, Text, CardPart]

    def test_the_end_of_an_answer_leaves_nothing_behind(self) -> None:
        turn = TurnParts(VOICE)
        turn.feed(Delta(text="The whole answer, in one piece"))

        assert len(turn.finish()) == 1
        assert turn.finish() == []

    def test_progress_is_a_hint_only_when_it_changes(self) -> None:
        parts = parts_of(
            BUTTON,
            Status(phase=Phase.UNDERSTANDING),
            Status(phase=Phase.RECALLING),
            Status(phase=Phase.SEARCHING),
            Status(phase=Phase.SEARCHING),
            Status(phase=Phase.ANSWERING),
        )

        assert parts == [
            StatusPart(state=StatusState.THINKING),
            StatusPart(state=StatusState.SEARCHING),
        ]

    def test_sources_are_a_card_only_for_a_screen_and_only_when_there_are_some(self) -> None:
        assert [type(p) for p in TurnParts(SCREEN).feed(sources(1))] == [CardPart]
        assert TurnParts(SCREEN).feed(Cited(citations=())) == []
        assert TurnParts(VOICE).feed(sources(1)) == []


class TestWhenAnAnswerFails:
    def test_what_was_written_is_said_and_then_a_word_that_it_stops_there(self) -> None:
        turn = TurnParts(VOICE)
        turn.feed(Delta(text="Hold the reset button for ten seconds. Then"))

        parts = turn.feed(Failed(code="gateway_unavailable", message="the model is away"))

        assert parts == [Speech(text="Then"), Speech(text="Sorry, I could not answer that.")]

    def test_a_reason_that_is_known_is_said_in_words(self) -> None:
        assert TurnParts(VOICE).failure("chat_not_configured") == [
            Speech(text="I have no model to answer with yet.")
        ]

    def test_a_screen_is_told_in_text_and_a_device_with_neither_is_told_nothing(self) -> None:
        assert TurnParts(SCREEN).failure("anything") == [
            Text(text="Sorry, I could not answer that.")
        ]
        assert TurnParts(BUTTON).failure("anything") == []
