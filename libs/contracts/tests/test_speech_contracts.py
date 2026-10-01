import pytest
from pydantic import ValidationError

from factotum.contracts.audio import AudioPayload
from factotum.contracts.conversation import MAX_MESSAGE_CHARS
from factotum.contracts.speech import (
    MAX_SPEECH_CHARS,
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


class TestSynthesizeResult:
    def test_it_holds_the_recording(self) -> None:
        assert SynthesizeResult(audio=payload()).audio == payload()

    def test_it_round_trips_through_json(self) -> None:
        result = SynthesizeResult(audio=payload(data=bytes(range(50))))

        assert SynthesizeResult.model_validate_json(result.model_dump_json()) == result
