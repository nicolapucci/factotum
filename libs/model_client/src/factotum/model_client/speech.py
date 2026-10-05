"""Speech, through the model gateway: a recording turned into text, and text turned into a
recording, each behind its own role and never knowing which engine does either."""

from collections.abc import AsyncGenerator
from typing import Protocol

import httpx
from pydantic import TypeAdapter, ValidationError

from factotum.contracts.audio import AudioPayload
from factotum.contracts.errors import GatewayError, GatewayUnavailableError
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole
from factotum.contracts.speech import (
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
from factotum.model_client.gateway import Delegation, error_from, typed_error

_EVENTS: TypeAdapter[SynthesisEventUnion] = TypeAdapter(SynthesisEvent)
_DATA = "data: "


class Speech(Protocol):
    async def transcribe(self, owner: Owner, audio: AudioPayload) -> str:
        """What was said in ``audio``. Raises ``SttNotConfiguredError`` when the owner has no
        speech-to-text model bound."""
        ...

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        """``text``, spoken aloud. Raises ``TtsNotConfiguredError`` when the owner has no
        text-to-speech model bound."""
        ...

    def synthesize_stream(
        self, owner: Owner, text: str
    ) -> AsyncGenerator[SynthesisFormat | SynthesisSamples]:
        """``text``, spoken aloud, as the voice makes it (ADR 0023): the format of its samples when
        it has any, then the samples, in order. A voice with nothing to say gives nothing. A
        failure is raised, typed, whenever it happens (``TtsNotConfiguredError`` when the owner has
        no text-to-speech model bound, when the stream is first read), and so is a stream that
        stopped before the voice said it was done: what a listener has been given is never ended as
        if it were whole."""
        ...


class GatewaySpeech:
    def __init__(self, client: httpx.AsyncClient, delegation: Delegation) -> None:
        self._client = client
        self._delegation = delegation

    async def transcribe(self, owner: Owner, audio: AudioPayload) -> str:
        body = TranscribeRequest(audio=audio).model_dump(mode="json")
        headers = dict(self._delegation.headers(owner))
        try:
            response = await self._client.post("/v1/transcribe", json=body, headers=headers)
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway could not be reached") from None
        if response.status_code != 200:
            raise error_from(response, ModelRole.STT)
        try:
            return TranscribeResult.model_validate(response.json()).text
        except (ValueError, ValidationError):
            raise GatewayError(
                "the model gateway answered with something that is not a transcription"
            ) from None

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        body = SynthesizeRequest(text=text).model_dump(mode="json")
        headers = dict(self._delegation.headers(owner))
        try:
            response = await self._client.post("/v1/synthesize", json=body, headers=headers)
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway could not be reached") from None
        if response.status_code != 200:
            raise error_from(response, ModelRole.TTS)
        try:
            return SynthesizeResult.model_validate(response.json()).audio
        except (ValueError, ValidationError):
            raise GatewayError(
                "the model gateway answered with something that is not an audio clip"
            ) from None

    async def synthesize_stream(
        self, owner: Owner, text: str
    ) -> AsyncGenerator[SynthesisFormat | SynthesisSamples]:
        body = SynthesizeRequest(text=text, stream=True).model_dump(mode="json")
        headers = dict(self._delegation.headers(owner))
        done = False
        try:
            async with self._client.stream(
                "POST", "/v1/synthesize", json=body, headers=headers
            ) as sent:
                if sent.status_code != 200:
                    await sent.aread()
                    raise error_from(sent, ModelRole.TTS)
                async for line in sent.aiter_lines():
                    if not line.startswith(_DATA):
                        continue
                    event = self._event(line[len(_DATA) :])
                    if isinstance(event, SynthesisDone):
                        done = True
                    elif isinstance(event, SynthesisError):
                        raise typed_error(event.code, event.message, ModelRole.TTS)
                    else:
                        yield event
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway stopped answering") from None
        if not done:
            raise GatewayUnavailableError("the model gateway stopped before the voice was done")

    @staticmethod
    def _event(payload: str) -> SynthesisEventUnion:
        try:
            return _EVENTS.validate_json(payload)
        except ValidationError:
            raise GatewayError(
                "the model gateway sent something that is not an event of a voice"
            ) from None
