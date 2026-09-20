from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from factotum.contracts.chat import Usage
from factotum.contracts.conversation import (
    MAX_MESSAGE_CHARS,
    Citation,
    Cited,
    Conversation,
    ConversationDetail,
    Delta,
    Exchange,
    Failed,
    Finished,
    HistoryHit,
    HistoryQuery,
    HistoryResult,
    Period,
    Phase,
    Reference,
    ReferenceKind,
    Started,
    Status,
    TurnEvent,
    TurnEventUnion,
    TurnRequest,
    TurnWarning,
    WarningCode,
)
from factotum.contracts.ids import CollectionId, ConversationId, DocumentId, ExchangeId

NOW = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)
EVENTS: TypeAdapter[TurnEvent] = TypeAdapter(TurnEventUnion)


def citation(**overrides: Any) -> Citation:
    fields: dict[str, Any] = {
        "number": 1,
        "document_id": DocumentId(uuid4()),
        "collection_id": CollectionId(uuid4()),
        "document_title": "Router manual",
    }
    return Citation(**{**fields, **overrides})


def exchange(**overrides: Any) -> Exchange:
    fields: dict[str, Any] = {
        "id": ExchangeId(uuid4()),
        "conversation_id": ConversationId(uuid4()),
        "seq": 1,
        "created_at": NOW,
        "question": "How do I reset the router?",
        "answer": "Hold the reset button for ten seconds.",
    }
    return Exchange(**{**fields, **overrides})


class TestTheDecisionAboutAMessage:
    @pytest.mark.parametrize("kind", ["none", "continuation", "earlier"])
    def test_the_kinds_are_three_plain_words(self, kind: str) -> None:
        assert Reference.model_validate({"kind": kind}).kind is ReferenceKind(kind)

    def test_an_earlier_exchange_can_be_looked_for_by_words_and_by_period(self) -> None:
        reference = Reference.model_validate(
            {"kind": "earlier", "topic": "the router recipe", "when": "last_week"}
        )

        assert (reference.topic, reference.when) == ("the router recipe", Period.LAST_WEEK)

    def test_a_kind_it_does_not_know_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Reference.model_validate({"kind": "sometimes"})

    def test_a_period_it_does_not_know_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Reference.model_validate({"kind": "earlier", "when": "a long time ago"})

    def test_the_schema_names_the_kinds(self) -> None:
        schema = Reference.model_json_schema()

        assert set(schema["$defs"]["ReferenceKind"]["enum"]) == {"none", "continuation", "earlier"}


class TestARequest:
    def test_a_message_is_enough(self) -> None:
        request = TurnRequest(message="  what does the warranty not cover?  ")

        assert request.message == "what does the warranty not cover?"
        assert request.conversation_id is None

    @pytest.mark.parametrize("message", ["", "   "])
    def test_a_blank_message_is_refused(self, message: str) -> None:
        with pytest.raises(ValidationError):
            TurnRequest(message=message)

    def test_a_message_too_long_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            TurnRequest(message="a" * (MAX_MESSAGE_CHARS + 1))

    def test_a_field_it_does_not_know_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            TurnRequest.model_validate({"message": "hi", "history": ["earlier"]})


class TestTheEventsOfATurn:
    @pytest.mark.parametrize(
        "event",
        [
            Started(conversation_id=ConversationId(uuid4()), exchange_id=ExchangeId(uuid4())),
            Status(phase=Phase.SEARCHING),
            Delta(text="Hold the "),
            Cited(citations=(citation(),)),
            TurnWarning(code=WarningCode.DOCUMENTS_UNAVAILABLE, message="they did not answer"),
            Finished(
                reference=ReferenceKind.EARLIER,
                exchanges_used=1,
                passages_used=2,
                untrusted_content=True,
                usage=Usage(input_tokens=10, output_tokens=5),
            ),
            Failed(code="provider_unavailable", message="the model did not answer"),
        ],
        ids=lambda event: event.type,
    )
    def test_each_event_survives_the_wire_as_the_same_kind(self, event: TurnEvent) -> None:
        assert EVENTS.validate_json(event.model_dump_json()) == event

    @pytest.mark.parametrize("chunk", [" world", "Hello ", " and "])
    def test_a_chunk_of_the_answer_keeps_the_spaces_at_its_edges(self, chunk: str) -> None:
        assert EVENTS.validate_json(Delta(text=chunk).model_dump_json()) == Delta(text=chunk)
        assert Delta(text=chunk).text == chunk

    def test_an_event_of_a_kind_nobody_sends_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            EVENTS.validate_json('{"type": "surprise"}')

    def test_a_finished_turn_says_whether_somebody_elses_words_were_in_it(self) -> None:
        with pytest.raises(ValidationError):
            Finished.model_validate({"reference": "none", "exchanges_used": 0, "passages_used": 0})


class TestWhatIsSaved:
    def test_an_exchange_is_a_question_and_an_answer_with_where_it_came_from(self) -> None:
        saved = exchange(citations=(citation(page=12, headings=("Router", "Reset")),))

        assert saved.citations[0].page == 12
        assert saved.untrusted is False

    def test_an_exchange_needs_a_time_zone(self) -> None:
        with pytest.raises(ValidationError):
            exchange(created_at=datetime(2026, 9, 21, 9, 30))  # noqa: DTZ001 - the point

    def test_a_citation_counts_from_one(self) -> None:
        with pytest.raises(ValidationError):
            citation(number=0)

    def test_a_conversation_says_how_many_exchanges_it_has(self) -> None:
        conversation = Conversation(
            id=ConversationId(uuid4()), started_at=NOW, last_at=NOW, exchanges=3
        )

        detail = ConversationDetail(conversation=conversation, exchanges=(exchange(),))

        assert (detail.conversation.exchanges, len(detail.exchanges)) == (3, 1)


class TestSearchingWhatWasSaid:
    def test_words_are_enough(self) -> None:
        assert HistoryQuery(query="router").limit == 5

    def test_a_period_is_enough(self) -> None:
        assert HistoryQuery(when=Period.YESTERDAY).query is None

    def test_neither_is_not_a_search(self) -> None:
        with pytest.raises(ValidationError, match="words to look for, a period"):
            HistoryQuery()

    @pytest.mark.parametrize("limit", [0, 21])
    def test_how_many_is_bounded(self, limit: int) -> None:
        with pytest.raises(ValidationError):
            HistoryQuery(query="router", limit=limit)

    def test_a_hit_says_how_it_was_found_and_what_is_left_empty_is_the_time_alone(self) -> None:
        by_time = HistoryHit(exchange=exchange(), score=1.0)
        by_both = HistoryHit(exchange=exchange(), score=0.5, matched_by=("vector", "text"))

        assert by_time.matched_by == ()
        assert by_both.matched_by == ("vector", "text")

    def test_a_score_is_a_share_of_the_best(self) -> None:
        with pytest.raises(ValidationError):
            HistoryHit(exchange=exchange(), score=0)

    def test_the_answer_can_say_it_was_less_good_than_it_could_be(self) -> None:
        result = HistoryResult(
            hits=(),
            warnings=(TurnWarning(code=WarningCode.HISTORY_WORDS_ONLY, message="words only"),),
        )

        assert result.warnings[0].code is WarningCode.HISTORY_WORDS_ONLY
