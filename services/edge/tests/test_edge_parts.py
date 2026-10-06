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
from factotum.edge.parts import TurnParts, sources_card
from factotum.edge.testing import answer_events, citation

VOICE = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT})
SCREEN = frozenset({Capability.DISPLAY})
BOTH = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.DISPLAY})
BUTTON = frozenset({Capability.BUTTONS, Capability.LED})


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

    def test_acting_is_a_hint_of_its_own(self) -> None:
        parts = parts_of(
            BUTTON,
            Status(phase=Phase.UNDERSTANDING),
            Status(phase=Phase.ACTING),
            Status(phase=Phase.ACTING),
            Status(phase=Phase.ANSWERING),
        )

        assert parts == [
            StatusPart(state=StatusState.THINKING),
            StatusPart(state=StatusState.ACTING),
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
