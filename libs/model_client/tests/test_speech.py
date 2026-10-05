import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import respx

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.audio import AudioPayload
from factotum.contracts.errors import (
    FactotumError,
    GatewayError,
    GatewayUnavailableError,
    ProviderUnavailableError,
    RateLimitedError,
    SttNotConfiguredError,
    TtsNotConfiguredError,
)
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.speech import (
    SynthesisDone,
    SynthesisError,
    SynthesisEventUnion,
    SynthesisFormat,
    SynthesisSamples,
)
from factotum.model_client import DevDelegation, GatewaySpeech

URL = "http://gateway.test"
USER = Owner(kind=OwnerKind.USER, id=uuid4())
TENANT = Owner(kind=OwnerKind.TENANT, id=uuid4())
RECORDING = AudioPayload(sample_rate=16000, data=b"a recording")
STREAM = {"content-type": "text/event-stream"}


def frames(*events: SynthesisEventUnion) -> bytes:
    return b"".join(f"data: {event.model_dump_json()}\n\n".encode() for event in events)


def error(code: str, message: str = "it failed") -> dict[str, object]:
    return {"error": {"code": code, "message": message, "details": {}}}


@pytest.fixture
async def speech() -> AsyncIterator[GatewaySpeech]:
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)
    async with httpx.AsyncClient(base_url=URL) as client:
        yield GatewaySpeech(client, DevDelegation(settings))


class Dropped(httpx.AsyncByteStream):
    """A response whose connection is lost after a few bytes."""

    def __init__(self, first: bytes) -> None:
        self._first = first

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        raise httpx.ReadError("the connection was reset")


async def heard(speech: GatewaySpeech, text: str = "hello") -> list[object]:
    return [event async for event in speech.synthesize_stream(USER, text)]


async def heard_until_it_failed(
    speech: GatewaySpeech, text: str = "hello"
) -> tuple[list[object], FactotumError | None]:
    """Everything the listener was given, and the error that ended it, if one did."""
    got: list[object] = []
    try:
        async for event in speech.synthesize_stream(USER, text):
            got.append(event)  # noqa: PERF401 - what came before a failure is what is kept
    except FactotumError as failure:
        return got, failure
    return got, None


class TestTranscribe:
    @respx.mock
    async def test_the_recording_goes_to_the_gateway_for_the_owner_and_the_words_come_back(
        self, speech: GatewaySpeech
    ) -> None:
        route = respx.post(f"{URL}/v1/transcribe").respond(200, json={"text": "how are you"})

        text = await speech.transcribe(TENANT, RECORDING)

        assert text == "how are you"
        sent = route.calls.last.request
        assert sent.headers["x-factotum-dev-subject"] == "tenant"
        assert AudioPayload.model_validate(json.loads(sent.content)["audio"]) == RECORDING

    @respx.mock
    async def test_no_model_for_the_role_is_no_speech_to_text(self, speech: GatewaySpeech) -> None:
        respx.post(f"{URL}/v1/transcribe").respond(404, json=error("binding_not_found"))

        with pytest.raises(SttNotConfiguredError):
            await speech.transcribe(USER, RECORDING)

    @respx.mock
    async def test_a_gateway_that_cannot_be_reached_is_unavailable(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/transcribe").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(GatewayUnavailableError):
            await speech.transcribe(USER, RECORDING)

    @respx.mock
    async def test_an_answer_that_is_not_a_transcription_is_refused(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/transcribe").respond(200, json={"nothing": "useful"})

        with pytest.raises(GatewayError, match="not a transcription"):
            await speech.transcribe(USER, RECORDING)


class TestSynthesize:
    @respx.mock
    async def test_the_text_goes_to_the_gateway_and_a_clip_comes_back(
        self, speech: GatewaySpeech
    ) -> None:
        route = respx.post(f"{URL}/v1/synthesize").respond(
            200, json={"audio": RECORDING.model_dump(mode="json")}
        )

        clip = await speech.synthesize(USER, "Dinner is ready")

        assert clip == RECORDING
        body = json.loads(route.calls.last.request.content)
        assert (body["text"], body["stream"]) == ("Dinner is ready", False)

    @respx.mock
    async def test_no_model_for_the_role_is_no_text_to_speech(self, speech: GatewaySpeech) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(404, json=error("binding_not_found"))

        with pytest.raises(TtsNotConfiguredError):
            await speech.synthesize(USER, "hello")

    @respx.mock
    async def test_an_answer_that_is_not_a_clip_is_refused(self, speech: GatewaySpeech) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(200, json={"nothing": "useful"})

        with pytest.raises(GatewayError, match="not an audio clip"):
            await speech.synthesize(USER, "hello")


class TestAStreamedVoice:
    @respx.mock
    async def test_the_format_and_the_samples_come_in_the_order_they_were_sent(
        self, speech: GatewaySpeech
    ) -> None:
        route = respx.post(f"{URL}/v1/synthesize").respond(
            200,
            content=frames(
                SynthesisFormat(sample_rate=22050),
                SynthesisSamples(data=b"\x00\x01\x02\x03"),
                SynthesisSamples(data=b"\x04\x05"),
                SynthesisDone(),
            ),
            headers=STREAM,
        )

        got = await heard(speech, "Dinner is ready")

        assert got == [
            SynthesisFormat(sample_rate=22050),
            SynthesisSamples(data=b"\x00\x01\x02\x03"),
            SynthesisSamples(data=b"\x04\x05"),
        ]
        sent = route.calls.last.request
        assert sent.headers["x-factotum-dev-subject"] == "user"
        body = json.loads(sent.content)
        assert (body["text"], body["stream"]) == ("Dinner is ready", True)

    @respx.mock
    async def test_a_voice_that_is_done_says_so_and_is_not_given_to_the_listener(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(
            200, content=frames(SynthesisFormat(sample_rate=16000), SynthesisDone()), headers=STREAM
        )

        got = await heard(speech)

        assert got == [SynthesisFormat(sample_rate=16000)]

    @respx.mock
    async def test_a_voice_with_nothing_to_say_is_done_and_gives_nothing(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(
            200, content=frames(SynthesisDone()), headers=STREAM
        )

        assert await heard(speech, "...") == []

    @respx.mock
    async def test_what_is_not_a_data_line_is_ignored(self, speech: GatewaySpeech) -> None:
        body = (
            b": a comment\n\nevent: ping\n\n"
            + frames(SynthesisFormat(sample_rate=16000))
            + frames(SynthesisDone())
        )
        respx.post(f"{URL}/v1/synthesize").respond(200, content=body, headers=STREAM)

        assert await heard(speech) == [SynthesisFormat(sample_rate=16000)]

    @respx.mock
    async def test_no_model_for_the_role_is_raised_when_the_stream_is_first_read(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(404, json=error("binding_not_found"))
        stream = speech.synthesize_stream(USER, "hello")  # nothing is asked until it is read

        with pytest.raises(TtsNotConfiguredError):
            await anext(stream)

    @respx.mock
    async def test_the_kind_of_a_failure_to_begin_survives_the_trip(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(502, json=error("provider_unavailable", "busy"))

        with pytest.raises(ProviderUnavailableError, match="busy"):
            await heard(speech)

    @respx.mock
    async def test_a_voice_that_fails_after_it_began_is_raised_after_what_it_had_said(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(
            200,
            content=frames(
                SynthesisFormat(sample_rate=22050),
                SynthesisSamples(data=b"\x00\x01"),
                SynthesisError(code="provider_unavailable", message="the voice fell over"),
            ),
            headers=STREAM,
        )

        got, failure = await heard_until_it_failed(speech)

        assert got == [SynthesisFormat(sample_rate=22050), SynthesisSamples(data=b"\x00\x01")]
        assert type(failure) is ProviderUnavailableError
        assert "the voice fell over" in str(failure)

    @respx.mock
    async def test_the_kind_of_a_failure_of_a_voice_is_the_one_its_code_says(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(
            200,
            content=frames(
                SynthesisFormat(sample_rate=22050),
                SynthesisError(code="rate_limited", message="slow down"),
            ),
            headers=STREAM,
        )

        with pytest.raises(RateLimitedError):
            await heard(speech)

    @respx.mock
    async def test_a_stream_that_stops_before_the_voice_is_done_is_not_a_voice_that_is(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(
            200,
            content=frames(SynthesisFormat(sample_rate=22050), SynthesisSamples(data=b"\x00\x01")),
            headers=STREAM,
        )

        got, failure = await heard_until_it_failed(speech)

        assert len(got) == 2
        assert type(failure) is GatewayUnavailableError
        assert "before the voice was done" in str(failure)

    @respx.mock
    async def test_an_answer_with_nothing_in_it_is_not_a_voice_that_is_done(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(200, content=b"", headers=STREAM)

        with pytest.raises(GatewayUnavailableError, match="before the voice was done"):
            await heard(speech)

    @respx.mock
    async def test_a_connection_that_is_lost_in_the_middle_is_a_gateway_that_stopped_answering(
        self, speech: GatewaySpeech
    ) -> None:
        first = frames(SynthesisFormat(sample_rate=22050))
        respx.post(f"{URL}/v1/synthesize").mock(
            return_value=httpx.Response(200, headers=STREAM, stream=Dropped(first))
        )

        got, failure = await heard_until_it_failed(speech)

        assert got == [SynthesisFormat(sample_rate=22050)]
        assert type(failure) is GatewayUnavailableError
        assert "stopped answering" in str(failure)

    @respx.mock
    async def test_a_gateway_that_cannot_be_reached_is_unavailable(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(GatewayUnavailableError, match="stopped answering"):
            await heard(speech)

    @respx.mock
    async def test_something_that_is_not_an_event_of_a_voice_is_refused(
        self, speech: GatewaySpeech
    ) -> None:
        body = b'data: {"type":"text_delta","text":"hi"}\n\n'
        respx.post(f"{URL}/v1/synthesize").respond(200, content=body, headers=STREAM)

        with pytest.raises(GatewayError, match="not an event of a voice"):
            await heard(speech)

    @respx.mock
    async def test_a_listener_that_goes_away_stops_reading_and_is_not_told_it_was_cut_off(
        self, speech: GatewaySpeech
    ) -> None:
        respx.post(f"{URL}/v1/synthesize").respond(
            200,
            content=frames(
                SynthesisFormat(sample_rate=22050),
                SynthesisSamples(data=b"\x00\x01"),
                SynthesisSamples(data=b"\x02\x03"),
                SynthesisDone(),
            ),
            headers=STREAM,
        )
        stream = speech.synthesize_stream(USER, "hello")

        first = await anext(stream)
        await stream.aclose()  # raises nothing: it is not a failure to stop listening

        assert first == SynthesisFormat(sample_rate=22050)
