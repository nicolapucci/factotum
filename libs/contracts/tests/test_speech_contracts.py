import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from factotum.contracts.audio import MAX_CHUNK_BYTES, AudioPayload, StreamFormat
from factotum.contracts.conversation import MAX_MESSAGE_CHARS
from factotum.contracts.speech import (
    MAX_SPEECH_CHARS,
    SynthesisDone,
    SynthesisError,
    SynthesisEvent,
    SynthesisEventUnion,
    SynthesisFormat,
    SynthesisSamples,
    SynthesizeRequest,
    SynthesizeResult,
    TranscribeRequest,
    TranscribeResult,
)


def payload(**changes: object) -> AudioPayload:
    fields: dict[str, object] = {"sample_rate": 16000, "data": b"some-wav-bytes"}
    return AudioPayload(**{**fields, **changes})  # type: ignore[arg-type]


class TestTranscribeRequest:
    def test_it_holds_the_recording(self) -> None:
        assert TranscribeRequest(audio=payload()).audio == payload()

    def test_it_round_trips_through_json(self) -> None:
        request = TranscribeRequest(audio=payload(data=b"\x00\x01\xff"))

        assert TranscribeRequest.model_validate_json(request.model_dump_json()) == request


class TestTranscribeResult:
    def test_what_was_heard_is_kept_as_it_was(self) -> None:
        assert TranscribeResult(text="how do I reset the router?").text == (
            "how do I reset the router?"
        )

    def test_nothing_heard_is_not_an_error(self) -> None:
        assert TranscribeResult(text="").text == ""

    def test_there_is_a_limit_like_any_other_utterance(self) -> None:
        with pytest.raises(ValidationError):
            TranscribeResult(text="w" * (MAX_MESSAGE_CHARS + 1))


class TestSynthesizeRequest:
    def test_it_holds_the_words(self) -> None:
        assert SynthesizeRequest(text="Dinner is ready").text == "Dinner is ready"

    def test_it_cannot_be_blank(self) -> None:
        with pytest.raises(ValidationError):
            SynthesizeRequest(text="")

    def test_there_is_a_limit_like_a_speech_part(self) -> None:
        with pytest.raises(ValidationError):
            SynthesizeRequest(text="w" * (MAX_SPEECH_CHARS + 1))

    def test_it_asks_for_a_clip_unless_it_says_otherwise(self) -> None:
        assert SynthesizeRequest(text="Dinner is ready").stream is False
        assert SynthesizeRequest(text="Dinner is ready", stream=True).stream is True

    def test_a_request_from_before_there_was_a_stream_is_still_one(self) -> None:
        assert SynthesizeRequest.model_validate_json('{"text": "Dinner is ready"}').stream is False


class TestSynthesizeResult:
    def test_it_holds_the_recording(self) -> None:
        assert SynthesizeResult(audio=payload()).audio == payload()

    def test_it_round_trips_through_json(self) -> None:
        result = SynthesizeResult(audio=payload(data=bytes(range(50))))

        assert SynthesizeResult.model_validate_json(result.model_dump_json()) == result


EVENTS: list[SynthesisEventUnion] = [
    SynthesisFormat(sample_rate=22050),
    SynthesisFormat(format=StreamFormat.PCM_S16LE, sample_rate=16000),
    SynthesisSamples(data=bytes(range(256))),
    SynthesisSamples(data=b"\x00\x01"),
    SynthesisDone(),
    SynthesisError(code="provider_unavailable", message="piper_local: could not synthesize"),
]

_EVENT: TypeAdapter[SynthesisEventUnion] = TypeAdapter(SynthesisEvent)


class TestTheEventsOfAStreamedVoice:
    @pytest.mark.parametrize("event", EVENTS, ids=lambda event: f"{event.type}")
    def test_every_event_survives_the_wire(self, event: SynthesisEventUnion) -> None:
        assert _EVENT.validate_json(event.model_dump_json()) == event

    def test_what_goes_over_the_wire_is_plain_json_with_the_samples_in_base64(self) -> None:
        assert json.loads(SynthesisFormat(sample_rate=22050).model_dump_json()) == {
            "type": "format",
            "format": "pcm_s16le",
            "sample_rate": 22050,
        }
        assert json.loads(SynthesisSamples(data=b"\x00\x01\x02\x03").model_dump_json()) == {
            "type": "samples",
            "data": "AAECAw==",
        }
        assert json.loads(SynthesisDone().model_dump_json()) == {"type": "done"}

    def test_the_samples_survive_the_wire_exactly(self) -> None:
        # Every byte value, including the ones that are not valid UTF-8 on their own.
        samples = SynthesisSamples(data=bytes(range(256)))

        restored = _EVENT.validate_json(samples.model_dump_json())

        assert isinstance(restored, SynthesisSamples)
        assert restored.data == bytes(range(256))

    @pytest.mark.parametrize("rate", [0, 7999, 48001])
    def test_a_rate_outside_what_speech_needs_is_refused(self, rate: int) -> None:
        with pytest.raises(ValidationError):
            SynthesisFormat(sample_rate=rate)

    def test_the_samples_are_16_bit_little_endian_unless_said_otherwise_and_nothing_else_is(
        self,
    ) -> None:
        assert SynthesisFormat(sample_rate=16000).format is StreamFormat.PCM_S16LE
        with pytest.raises(ValidationError):
            _EVENT.validate_json('{"type": "format", "format": "mp3", "sample_rate": 16000}')

    @pytest.mark.parametrize("size", [0, 1, 3, 4001])
    def test_a_piece_is_whole_samples_and_is_not_nothing(self, size: int) -> None:
        with pytest.raises(ValidationError):
            SynthesisSamples(data=bytes(size))

    def test_a_piece_is_at_most_what_a_chunk_of_a_stream_holds(self) -> None:
        assert len(SynthesisSamples(data=bytes(MAX_CHUNK_BYTES)).data) == MAX_CHUNK_BYTES
        with pytest.raises(ValidationError):
            SynthesisSamples(data=bytes(MAX_CHUNK_BYTES + 2))

    @pytest.mark.parametrize(
        "wire",
        [
            "{}",
            '{"type": "pause"}',
            '{"type": "samples"}',
            '{"type": "samples", "data": "not base64 !"}',
            '{"type": "format"}',
            '{"type": "done", "extra": 1}',
            '{"type": "error", "code": "x"}',
        ],
    )
    def test_what_is_not_an_event_is_refused(self, wire: str) -> None:
        with pytest.raises(ValidationError):
            _EVENT.validate_json(wire)

    @given(data=st.binary(min_size=2, max_size=2000).map(lambda b: b[: len(b) - len(b) % 2]))
    def test_whatever_the_samples_they_arrive_as_they_were_sent(self, data: bytes) -> None:
        event = SynthesisSamples(data=data)

        assert _EVENT.validate_json(event.model_dump_json()) == event
